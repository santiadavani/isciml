import os
import numpy as np
from tqdm import tqdm

N = 32
samples_dir = "/tmp/samples/"
targets_dir = "/tmp/targets/"

for ii in tqdm(range(N)):
    sample = np.random.uniform(size=(76800,))
    target = np.random.uniform(size=(76800,))
    np.save(samples_dir + "/sample_%d.npy" % ii, sample)
    np.save(targets_dir + "/adj_sample_%d.npy" % ii, target)
