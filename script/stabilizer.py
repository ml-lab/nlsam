#! /usr/bin/env python

from __future__ import division, print_function

import nibabel as nib
import numpy as np

import os
import argparse

from itertools import repeat
from multiprocessing import Pool, cpu_count

from dipy.core.ndindex import ndindex
from dipy.core.gradients import gradient_table
from dipy.io.gradients import read_bvals_bvecs
from dipy.denoise.nlmeans import nlmeans
from dipy.denoise.noise_estimate import estimate_sigma, piesno

from scipy.ndimage.filters import convolve

from nlsam.smoothing import local_standard_deviation, sh_smooth
from nlsam.stabilizer import chi_to_gauss, fixed_point_finder, corrected_sigma


DESCRIPTION = """
    Script to transform noisy rician/non central chi signals into
    gaussian distributed signals.

    Reference:
    [1]. Koay CG, Ozarslan E and Basser PJ.
    A signal transformational framework for breaking the noise floor
    and its applications in MRI.
    Journal of Magnetic Resonance 2009; 197: 108-119.
    """


def buildArgsParser():

    p = argparse.ArgumentParser(description=DESCRIPTION,
                                formatter_class=argparse.RawTextHelpFormatter)

    p.add_argument('input', action='store', metavar='input',
                   help='Path of the image file to stabilize.')

    p.add_argument('-N', action='store', dest='N',
                   metavar='int', required=True, type=int,
                   help='Number of receiver coils of the scanner. \n' +
                   'Use N=1 in the case of a SENSE (GE, Phillips) reconstruction and \n' +
                   'N >= 1 for GRAPPA reconstruction (Siemens).')

    p.add_argument('-o', action='store', dest='savename',
                   metavar='string', required=False, default=None, type=str,
                   help='Path and prefix for the saved transformed file. \n' +
                   'The name is always appended with _stabilized.nii.gz')

    p.add_argument('--cores', action='store', dest='n_cores',
                   metavar='int', required=False, default=None, type=int,
                   help='Number of cores to use for multiprocessing.')

    p.add_argument('--mask', action='store', dest='mask',
                   metavar='string', default=None, type=str,
                   help='Path to a binary mask. Only the data inside the \n' +
                        'mask will be used for computations.')

    p.add_argument('--noise_est', action='store', dest='noise_method',
                   metavar='string', required=False, default='local_std', type=str,
                   help='Noise estimation method used for estimating sigma. \n' +
                   'local_std (default) : Compute local noise standard deviation with correction factor. \n' +
                   'No a priori needed.\n' +
                   'piesno : Use PIESNO estimation, assumes the presence of background \n' +
                   'in the data.')

    p.add_argument('--smooth', action='store', dest='smooth_method',
                   metavar='string', required=False, default='sh_smooth', type=str,
                   help='Smoothing method used for initialising m_hat.\n' +
                   'local_mean : Compute 3D local mean, might blur edges.\n' +
                   'non_local_means : Compute 3D nlmeans from dipy, slower but does not blur edges.\n' +
                   'sh_smooth (default): Fit SH for smoothing the raw signal. Really fast, and does not overblur.\n' +
                   'Also requires the bvals/bvecs to be given\n' +
                   'no_smoothing : Just use the data as-is for initialisation.')

    p.add_argument('--bvals', action='store', dest='bvals',
                   metavar='bvals', type=str, default='',
                   help='Path of the bvals file, in FSL format. \n' +
                   'Required for -smooth_method sh_smooth')

    p.add_argument('--bvecs', action='store', dest='bvecs',
                   metavar='bvecs', type=str, default='',
                   help='Path of the bvecs file, in FSL format. \n' +
                   'Required for -smooth_method sh_smooth')

    return p


def helper(arglist):
    """Helper function for multiprocessing the stabilization part."""

    data, m_hat, mask, sigma, N = arglist
    out = np.zeros(data.shape, dtype=np.float32)

    for idx in ndindex(data.shape):

        if sigma[idx] > 0 and mask[idx]:
            eta = fixed_point_finder(m_hat[idx], sigma[idx], N)
            out[idx] = chi_to_gauss(data[idx], eta, sigma[idx], N)

    return out


def main():

    parser = buildArgsParser()
    args = parser.parse_args()

    vol = nib.load(args.input)
    data = vol.get_data()
    affine = vol.get_affine()

    if args.mask is None:
        mask = np.ones(data.shape[:-1], dtype=np.bool)
    else:
        mask = nib.load(args.mask).get_data().astype(np.bool)

    N = args.N

    if args.n_cores is None:
        n_cores = cpu_count()
    else:
        n_cores = args.n_cores

    noise_method = args.noise_method
    smooth_method = args.smooth_method

    # Since negatives are allowed, convert uint to int
    if data.dtype.kind == 'u':
        dtype = data.dtype.name[1:]
    else:
        dtype = data.dtype

    if args.savename is None:
        if os.path.basename(args.input).endswith('.nii'):
            temp = os.path.basename(args.input)[:-4]
        elif os.path.basename(args.input).endswith('.nii.gz'):
            temp = os.path.basename(args.input)[:-7]

        filename = os.path.split(os.path.abspath(args.input))[0] + '/' + temp
        print("savename is", filename)

    else:
        filename = args.savename

    print("Estimating m_hat with method " + smooth_method)

    if smooth_method == 'local_mean':
        m_hat = np.zeros_like(data, dtype=np.float32)
        size = (3, 3, 3)
        k = np.ones(size) / np.sum(size)
        conv_out = np.zeros_like(data[..., 0], dtype=np.float64)

        for idx in range(data.shape[-1]):
            convolve(data[..., idx], k, mode='reflect', output=conv_out)
            m_hat[..., idx] = conv_out

    elif smooth_method == 'nlmeans':
        nlmeans_sigma = estimate_sigma(data)
        m_hat = nlmeans(data, nlmeans_sigma, rician=False, mask=mask)

    elif smooth_method == 'no_smoothing':
        m_hat = np.array(data, copy=True, dtype=np.float32)

    elif smooth_method == 'sh_smooth':
        bvals, bvecs = read_bvals_bvecs(args.bvals, args.bvecs)
        gtab = gradient_table(bvals, bvecs)
        m_hat = sh_smooth(data, gtab, sh_order=4)
    else:
        raise ValueError("method " + smooth_method + " is not recognized!")

    nib.save(nib.Nifti1Image(m_hat, affine), filename + "_m_hat.nii.gz")
    nib.save(nib.Nifti1Image(np.abs(data-m_hat), affine), filename + "_diff.nii.gz")

    print("Estimating noise with method " + noise_method)

    if noise_method == 'piesno':
        # This will need an update once this PR is merged in dipy
        # https://github.com/nipy/dipy/pull/390
        sigma = np.zeros_like(data, dtype=np.float32)
        mask_noise = np.zeros(data.shape[:-1], dtype=np.bool)

        for idx in range(data.shape[1]):
            print("Now processing coronal slice", idx+1, "out of", data.shape[1])
            sigma[:, idx, ...], mask_noise[:, idx, :] = piesno(data[:, idx, ...],  N=N, return_mask=True)

        nib.save(nib.Nifti1Image(mask_noise.astype(np.int16), affine), filename + "_mask_noise.nii.gz")

    elif noise_method == 'local_std':
        sigma_3D = local_standard_deviation(data, n_cores=n_cores)

        # Compute the corrected value for each 3D volume
        sigma = corrected_sigma(m_hat, np.repeat(sigma_3D[..., None], data.shape[-1], axis=-1),
                                np.repeat(mask[..., None], data.shape[-1], axis=-1), N, n_cores=n_cores)

    else:
        raise ValueError("method " + noise_method + " is not recognized!")

    nib.save(nib.Nifti1Image(sigma, affine), filename + "_sigma.nii.gz")

    print("Now performing stabilisation")

    pool = Pool(processes=n_cores)
    arglist = [(data_vox, m_hat_vox, mask_vox, sigma_vox, N_vox)
               for data_vox, m_hat_vox, mask_vox, sigma_vox, N_vox
               in zip(data, m_hat, np.repeat(mask[..., None], data.shape[-1], axis=-1), sigma, repeat(N))]
    data_stabilized = pool.map(helper, arglist)
    pool.close()
    pool.join()

    nib.save(nib.Nifti1Image(np.asarray(data_stabilized).reshape(data.shape).astype(dtype), affine), filename + "_stabilized.nii.gz")


if __name__ == "__main__":
    main()