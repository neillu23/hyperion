#!/usr/bin/env python

"""
Trains Q-scoring back-end
"""
from __future__ import absolute_import
from __future__ import print_function
from __future__ import division
from six.moves import xrange

import sys
import os
import argparse
import time

import numpy as np

from hyperion.helpers import VectorClassReader as VCR
from hyperion.transforms import TransformList
from hyperion.classifiers import QScoringHomoGBE as GBE


def train_qscoring_backend(iv_file, train_list, preproc_file,
                           output_path, **kwargs):
    
    if preproc_file is not None:
        preproc = TransformList.load(preproc_file)
    else:
        preproc = None

    vcr_args = VCR.filter_args(**kwargs)
    vcr_train = VCR(iv_file, train_list, preproc, **vcr_args)
    x, class_ids = vcr_train.read()

    t1 = time.time()

    model_args = GBE.filter_train_args(**kwargs)
    model = GBE(**model_args)
    model.fit(x, class_ids)
    print('Elapsed time: %.2f s.' % (time.time()-t1))

    model.save(output_path)
    
    

if __name__ == "__main__":

    parser=argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        fromfile_prefix_chars='@',
        description='Trains Q-scoring back-end')

    parser.add_argument('--iv-file', dest='iv_file', required=True)
    parser.add_argument('--train-list', dest='train_list', required=True)
    parser.add_argument('--preproc-file', dest='preproc_file', default=None)

    VCR.add_argparse_args(parser)
    GBE.add_argparse_train_args(parser)

    parser.add_argument('--output-path', dest='output_path', required=True)
    
    args=parser.parse_args()
    
    train_qscoring_backend(**vars(args))

            