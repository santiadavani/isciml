import click
import numpy as np
import pandas as pd
import logging
from rich.logging import RichHandler
from rich.progress import track
from tqdm import tqdm
import sys
import yaml
import os
import pyvista as pv
import adjoint
import forward
from typing import Union, List, Literal
from mpi4py import MPI
import ctypes
import glob

comm = MPI.COMM_WORLD
rank = comm.Get_rank()
size = comm.Get_size()

FORMAT = "%(message)s"
logging.basicConfig(
    level="NOTSET",
    format="Rank: " + str(rank) + "/" + str(size) + ": %(asctime)s - %(message)s",
    datefmt="[%X]",
    handlers=[RichHandler()],
)
log = logging.getLogger("rich")


class Mesh:
    def __init__(self, vtk_file_name: Union[str, os.PathLike]):
        if not rank:
            log.debug("Reading vtk file %s" % vtk_file_name)
        if os.path.exists(vtk_file_name):
            self.vtk_file_name = vtk_file_name
        else:
            msg = "VTK file %s does not exist" % vtk_file_name
            log.error(msg)
            raise ValueError(msg)
        try:
            self.mesh = pv.read(self.vtk_file_name)
        except Exception as e:
            log.error(e)
            raise ValueError(e)
        if not rank:
            log.debug("Reading mesh completed")

        self.npts = self.mesh.n_points
        self.ncells = self.mesh.n_cells
        self.nodes = np.array(self.mesh.points)
        self.tet_nodes = self.mesh.cell_connectivity.reshape((-1, 4))
        if not rank:
            log.debug("Generated mesh properties")

    def __str__(self):
        return str(self.mesh)

    def get_centroids(self):
        if not rank:
            log.debug("Getting centroids")
        nk = self.tet_nodes[:, 0]
        nl = self.tet_nodes[:, 1]
        nm = self.tet_nodes[:, 2]
        nn = self.tet_nodes[:, 3]
        self.centroids = (
            self.nodes[nk, :]
            + self.nodes[nl, :]
            + self.nodes[nm, :]
            + self.nodes[nn, :]
        ) / 4.0
        if not rank:
            log.debug("Getting centroids done!")

    def get_volumes(self):
        if not rank:
            log.debug("Getting volumes")
        ntt = len(self.tet_nodes)
        vot = np.zeros((ntt))
        for itet in np.arange(0, ntt):
            n1 = self.tet_nodes[itet, 0]
            n2 = self.tet_nodes[itet, 1]
            n3 = self.tet_nodes[itet, 2]
            n4 = self.tet_nodes[itet, 3]
            x1 = self.nodes[n1, 0]
            y1 = self.nodes[n1, 1]
            z1 = self.nodes[n1, 2]
            x2 = self.nodes[n2, 0]
            y2 = self.nodes[n2, 1]
            z2 = self.nodes[n2, 2]
            x3 = self.nodes[n3, 0]
            y3 = self.nodes[n3, 1]
            z3 = self.nodes[n3, 2]
            x4 = self.nodes[n4, 0]
            y4 = self.nodes[n4, 1]
            z4 = self.nodes[n4, 2]
            pv = (
                (x4 - x1) * ((y2 - y1) * (z3 - z1) - (z2 - z1) * (y3 - y1))
                + (y4 - y1) * ((z2 - z1) * (x3 - x1) - (x2 - x1) * (z3 - z1))
                + (z4 - z1) * ((x2 - x1) * (y3 - y1) - (y2 - y1) * (x3 - x1))
            )
            vot[itet] = np.abs(pv / 6.0)
        self.volumes = vot
        if not rank:
            log.debug("Getting volumes done!")


class MagneticProperties:
    def __init__(
        self, file_name: Union[str, os.PathLike], ambient_magnetic_field: List[float]
    ):
        log.debug("Reading magnetic properties %s" % file_name)
        if os.path.exists(file_name):
            self.file_name = file_name
        else:
            msg = "File %s does not exist" % file_name
            log.error(msg)
            raise ValueError(msg)

        try:
            self.properties = np.load(file_name)
        except Exception as e:
            log.error(e)
            raise ValueError(e)
        log.debug("Reading magnetic properties %s done!" % file_name)

        if len(self.properties.shape) > 0:
            self.n_cells = self.properties.shape[0]
        else:
            msg = "Magnetic properties file %s is incorrect" % file_name
            log.error(msg)
            raise ValueError(msg)

        if self.properties.ndim == 1:
            self.properties = np.expand_dims(self.properties, axis=1)

        if self.properties.shape[1] > 0:
            self.susceptibility = self.properties[:, 0]

        Bx = ambient_magnetic_field[0]
        By = ambient_magnetic_field[1]
        Bz = ambient_magnetic_field[2]
        Bv = np.sqrt(Bx**2 + By**2 + Bz**2)

        if self.properties.shape[1] > 1:
            self.kx = self.properties[:, 1]
        else:
            self.kx = Bx / Bv

        if self.properties.shape[1] > 2:
            self.ky = self.properties[:, 2]
        else:
            self.ky = By / Bv

        if self.properties.shape[1] > 3:
            self.kz = self.properties[:, 3]
        else:
            self.kz = Bz / Bv

        log.debug("Setting all magnetic properties done!")


class MagneticSolver:
    def __init__(
        self,
        reciever_file_name: Union[str, os.PathLike],
        ambient_magnetic_field: List[float],
        header: Union[int, List[int], None] = None,
    ):
        if not rank:
            log.debug("Solver initialization started!")
        if os.path.exists(reciever_file_name):
            self.reciever_file_name = reciever_file_name
        else:
            msg = "File %s does not exist" % reciever_file_name
            if not rank:
                log.error(msg)
            raise ValueError(msg)

        if not header:
            header = None

        try:
            self.receiver_locations = pd.read_csv(reciever_file_name, header=header)
        except Exception as e:
            log.error(e)
            raise ValueError(e)

        if len(ambient_magnetic_field) != 3:
            msg = (
                "Length of ambient magnetic field has to be exactly 3, passed a length of %d"
                % len(ambient_magnetic_field)
            )
            if not rank:
                log.error(msg)
            raise ValueError(msg)

        self.Bx = ambient_magnetic_field[0]
        self.By = ambient_magnetic_field[1]
        self.Bz = ambient_magnetic_field[2]
        self.Bv = np.sqrt(self.Bx**2 + self.By**2 + self.Bz**2)

        self.LX = self.Bx / self.Bv
        self.LY = self.By / self.Bv
        self.LZ = self.Bz / self.Bv
        if not rank:
            log.debug("Solver initialization done!")

    def solve(
        self,
        mesh: Mesh,
        magnetic_properties: MagneticProperties,
        mode: Literal["adjoint", "forward"],
    ) -> np.ndarray:
        rho_sus = np.zeros((1000000), dtype=float)
        rho_sus[0 : mesh.ncells] = magnetic_properties.susceptibility

        ctet = np.zeros((1000000, 3), dtype=float)
        ctet[0 : mesh.ncells] = mesh.centroids

        vtet = np.zeros((1000000), dtype=float)
        vtet[0 : mesh.ncells] = mesh.volumes

        nodes = np.zeros((1000000, 3), dtype=float)
        nodes[0 : mesh.npts] = mesh.nodes

        tets = np.zeros((1000000, 4), dtype=int)
        tets[0 : mesh.ncells] = mesh.tet_nodes + 1

        n_obs = len(self.receiver_locations)
        rx_loc = self.receiver_locations.to_numpy()

        ismag = True
        rho_sus = rho_sus * self.Bv

        istensor = False

        obs_pts = np.zeros((1000000, 3), dtype=float)
        obs_pts[0:n_obs] = rx_loc[:, 0:3]

        if mode == "adjoint":
            if (
                isinstance(magnetic_properties.kx, float)
                and isinstance(magnetic_properties.ky, float)
                and isinstance(magnetic_properties.kz, float)
            ):
                log.debug(
                    "Adjoint solver started for %s" % magnetic_properties.file_name
                )
                adjoint_output = adjoint.adjoint(
                    rho_sus,
                    ismag,  # this calls a function calc_and_mig_field
                    istensor,  # We input all the arrays required
                    magnetic_properties.kx,
                    magnetic_properties.ky,
                    magnetic_properties.kz,
                    self.LX,
                    self.LY,
                    self.LZ,
                    nodes,
                    tets,
                    mesh.ncells,
                    obs_pts,
                    n_obs,
                    ctet,
                    vtet,
                )
                log.debug("Adjoint solver done for %s" % magnetic_properties.file_name)
                output = adjoint_output[0 : mesh.ncells]
            else:
                msg = (
                    "Expecting float for kx, ky, kz values but recieved kx = %s, ky = %s, kz = %s"
                    % (
                        str(
                            type(magnetic_properties.kx),
                            str(type(magnetic_properties.ky)),
                            str(type(magnetic_properties.kz)),
                        )
                    )
                )
                log.error(msg)
                raise ValueError(msg)
        else:
            log.debug(
                "Forward solver in progress for %s" % magnetic_properties.file_name
            )
            kx = np.zeros((1000000), dtype=float)
            kx[0 : mesh.ncells] = magnetic_properties.kx

            ky = np.zeros((1000000), dtype=float)
            ky[0 : mesh.ncells] = magnetic_properties.ky

            kz = np.zeros((1000000), dtype=float)
            kz[0 : mesh.ncells] = magnetic_properties.kz

            forward_output = forward.forward(
                rho_sus,
                ismag,
                istensor,
                kx,
                ky,
                kz,
                self.LX,
                self.LY,
                self.LZ,
                nodes,
                tets,
                mesh.ncells,
                obs_pts,
                n_obs,
            )
            output = forward_output[0:n_obs]
            log.debug("Forward solver is done for %s" % magnetic_properties.file_name)
        return output


@click.command()
@click.option(
    "--vtk",
    help="Mesh in vtk file format",
    type=click.Path(),
    required=True,
)
@click.option(
    "--receiver_file",
    help="Receiver locations file in CSV format",
    type=click.Path(),
    required=True,
)
@click.option(
    "--receiver_header",
    help="Header parameter is Union[int, List[int], None]",
    type=int,
    multiple=True,
    default=None,
    show_default=True,
)
@click.option(
    "--input_folder",
    help="Folder with files containing magnetic properties in numpy format",
    type=click.Path(),
    required=True,
)
@click.option(
    "--ambient_field",
    nargs=3,
    type=click.Tuple([float, float, float]),
    default=(820.5, 16241.7, 53380.0),
    show_default=True,
)
@click.option(
    "--output_folder",
    help="Folder with files containing adjoints or forward in numpy format",
    type=click.Path(),
    required=True,
)
@click.option(
    "--output_prefix",
    help='Output prefix for the file names will be appended to input files. Defaults to adjoint for "adjoint" for adjoint mode and "forward" for forward mode',
    default="",
)
@click.option(
    "--solver",
    help="Solver mode adjoint or forward",
    type=click.Choice(["adjoint", "forward"]),
    default="adjoint",
    show_default=True,
)
def isciml(**kwargs):
    mesh = Mesh(kwargs["vtk"])
    mesh.get_centroids()
    mesh.get_volumes()

    solver = MagneticSolver(
        kwargs["receiver_file"],
        kwargs["ambient_field"],
        kwargs["receiver_header"],
    )

    output_folder = kwargs["output_folder"]
    if os.path.exists(output_folder):
        if os.listdir(output_folder):
            msg = "Output folder %s is not empty - exiting" % output_folder
            if not rank:
                log.error(msg)
            sys.exit(1)
    else:
        msg = "Output folder %s does not exist - creating.. " % output_folder
        if not rank:
            log.debug(msg)
        os.mkdir(output_folder)

    # Reading magnetic properites files and distributing them across processes
    if os.path.exists(kwargs["input_folder"]):
        numpy_files = glob.glob(kwargs["input_folder"] + "/*.npy")
    else:
        msg = "Folder %s does not exist or readable" % kwargs["input_folder"]
        log.error(msg)
        sys.exit(1)

    total_files = len(numpy_files)

    if size > total_files:
        msg = (
            "Number of processes (%d) > Number of files (%d). Please make sure that np <= total files"
            % (size, total_files)
        )
        log.error(msg)
        sys.exit(1)

    files_per_proc = total_files / size
    start_file_index = int(rank * files_per_proc)

    if rank == (size - 1):
        end_file_index = total_files
    else:
        end_file_index = int((rank + 1) * files_per_proc)
    log.info(
        "start_file_index = %d, end_file_index = %d"
        % (start_file_index, end_file_index)
    )
    log.info(
        "Processing %d files " % (len(numpy_files[start_file_index:end_file_index]))
    )

    for _file in track(
        numpy_files[start_file_index:end_file_index], description="Rank %d" % rank
    ):
        properties = MagneticProperties(_file, kwargs["ambient_field"])
        output = solver.solve(mesh, properties, mode=kwargs["solver"])
        _file_name = _file.split("/")[-1]

        output_prefix = kwargs["output_prefix"]
        if not output_prefix:
            output_prefix = kwargs["solver"]

        adjoint_file_name = output_folder + "/" + output_prefix + "_" + _file_name
        np.save(adjoint_file_name, output)

    return 0


if __name__ == "__main__":
    sys.exit(isciml())  # pragma: no cover
