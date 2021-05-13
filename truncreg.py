"""
Script for running random.
"""

import subprocess 
import torch as ch
from torch import Tensor
from torch.distributions import Uniform
import pandas as pd
import numpy as np
import csv
import json
from cox.utils import Parameters
from cox.store import Store
import os
from sklearn.linear_model import LinearRegression
from argparse import ArgumentParser

from delphi.stats.linear_regression import TruncatedLinearRegression
from delphi.oracle import Left
from delphi.utils import constants as consts

# CONSTANTS 
TABLE_NAME = 'results'

# commands and arguments
COMMAND = 'Rscript'
PATH2SCRIPT = '/home/gridsan/stefanou/delphi/truncreg.R'
TMP_FILE = 'tmp.csv'
RESULT_FILE = 'result.csv'

# experiment argument parser
parser = ArgumentParser()
parser.add_argument('--dims', type=int, required=True, help='dimensions to run regression alg')
parser.add_argument('--bias', action='store_true', help='bias term for regression')
parser.add_argument('--out-dir', required=True, help='experiment output')
parser.add_argument('--samples', type=int, required=False, default=10000,  help='number of samples to generate for ground-truth')
parser.add_argument('--c', type=float, required=False, default=0, help='truncation parameter for experiment')
parser.add_argument('--batch_size', type=int, required=False, default=100, help='batch size for procedure')
parser.add_argument('--lr', type=float, required=False, default=1e-1, help='learning rate for weight params')
parser.add_argument('--var-lr', type=float, required=False, default=1e-2, help='learning rate for variance parameter')
parser.add_argument('--var', type=int, required=False, default=20, help='maximum variance to run for experiment')
parser.add_argument('--trials', type=int, required=False, default=10, help='number of trials to run')


def main(args):
    # MSE Loss
    mse_loss = ch.nn.MSELoss()

    # distribution for generating ground truth
    U = Uniform(args.lower, args.upper)
    U_ = Uniform(args.x_lower, args.x_upper)

    # set experiment manual seed 
    ch.manual_seed(0)
    for i in range(args.trials):
        # create store and add table
        store = Store(args.out_dir)
        store.add_table(TABLE_NAME, { 
            'known_param_mse': float,
            'unknown_param_mse': float,
            'unknown_var_mse': float,
            'ols_param_mse': float,
            'ols_var_mse': float,
            'trunc_reg_param_mse': float, 
            'trunc_var_mse': float,
            'alpha': float, 
            'var': float, 
        })

        # increase variance up to 20
        for var in range(1, args.var + 1):
            # generate ground truth
            ground_truth = ch.nn.Linear(in_features=args.dims, out_features=1, bias=args.bias)
            ground_truth.weight = ch.nn.Parameter(U.sample(ch.Size([1, args.dims]))) 
            # bias term 
            if args.bias: 
                ground_truth.bias = ch.nn.Parameter(U.sample(ch.Size([1, 1])))

            # remove synthetic data from the computation graph
            with ch.no_grad():
                # generate data
                X = U_.sample(ch.Size([args.samples, args.dims]))
                y = ground_truth(X) + ch.sqrt(Tensor([var])) * ch.randn(X.size(0), 1)
                # truncate
                indices = args.phi(y).nonzero(as_tuple=False).flatten()
                y_trunc, x_trunc = y[indices], X[indices]
                alpha = Tensor([y_trunc.size(0) / args.samples])

            # empirical linear regression
            ols = LinearRegression() 
            ols.fit(x_trunc, y_trunc)
            ols_var = ch.var(Tensor(ols.predict(x_trunc)) - y_trunc, dim=0)[..., None]

            # truncated linear regression with known noise variance
            trunc_reg = TruncatedLinearRegression(phi=args.phi, alpha=alpha, args=args, bias=args.bias, var=ols_var)
            results = trunc_reg.fit(x_trunc, y_trunc)
            w_, w0_ = results.weight.detach().cpu(), results.bias.detach().cpu()

            # truncated linear regression with unknown noise variance
            trunc_reg = TruncatedLinearRegression(phi=args.phi, alpha=alpha, args=args, bias=args.bias)
            results = trunc_reg.fit(x_trunc, y_trunc)
            var_ = results.lambda_.inverse().detach()
            w, w0 = (results.v.detach()*var_).cpu(), (results.bias.detach()*var_).cpu()

            # spawn subprocess to run truncreg experiment
            concat = ch.cat([x_trunc, y_trunc], dim=1).numpy()
            """
            DATA FORMAT:
                -First n-1 columns are independent variables
                -nth column is dependent variable
            """
            concat_df = pd.DataFrame(concat)
            concat_df.to_csv(args.out_dir + '/' + TMP_FILE) # save data to csv
            """
            Arguments
            - c - truncation point (float)
            - dir - left or right -> type of truncation (str)
            """
            cmd = [COMMAND, PATH2SCRIPT] + [str(args.C), 'left', args.out_dir]

            # check_output will run the command and store the result
            result = subprocess.check_output(cmd, universal_newlines=True)
            trunc_res = Tensor(pd.read_csv(args.out_dir + '/' + RESULT_FILE)['x'].to_numpy())[None,...]

            # parameter estimates 
            known_params = ch.cat([w_.T, w0_[..., None]])

            real_params = ch.cat([ground_truth.weight.T, ground_truth.bias])
            ols_params = ch.cat([Tensor(ols.coef_).T, Tensor(ols.intercept_)[..., None]])
            unknown_params = ch.cat([w, w0])
            trunc_reg_params = ch.cat([trunc_res[0][1:-1].flatten(), trunc_res[0][0]])

            # metrics
            known_param_mse = mse_loss(known_params, real_params)
            unknown_param_mse = mse_loss(unknown_params, real_params)
            print("ols var size: ", Tensor([var]).size())
            print("unknown var size:", var_.size())
            unknown_var_mse = mse_loss(var_, Tensor([var]))
            print("unknown var")
            ols_param_mse = mse_loss(Tensor(ols_params), Tensor(real_params))
            ols_var_mse = mse_loss(ols_var, Tensor([var]))
            print("trunc reg param size: ", trunc_reg_params.size())
            print("real params size: ", real_params.size())
            trunc_reg_param_mse = mse_loss(trunc_reg_params, real_params)
            print("trunc reg param")
            trunc_var_mse = mse_loss(trunc_res[0][-1].pow(2), Tensor([var]))
            print("trunc var size: ", trunc_res[0][-1].size())

            # add results to store
            store[TABLE_NAME].append_row({ 
                'known_param_mse': known_param_mse,
                'unknown_param_mse': unknown_param_mse,
                'unknown_var_mse': unknown_var_mse,
                'ols_param_mse': ols_param_mse,
                'ols_var_mse': ols_var_mse,
                'trunc_reg_param_mse': trunc_reg_param_mse, 
                'trunc_var_mse': trunc_var_mse,
                'alpha': float(alpha.flatten()),
                'var': float(var), 
            })

        # close current store
        store.close()

if __name__ == '__main__': 
    # set environment variable so that stores can create output files
    os.environ['HDF5_USE_FILE_LOCKING'] = 'FALSE'

    args = Parameters(parser.parse_args().__dict__)
    args.__setattr__('workers', 8)
    args.__setattr__('custom_lr_multiplier', consts.COSINE)
    # independent variable bounds
    args.__setattr__('x_lower', -5)
    args.__setattr__('x_upper', 5)
    # parameter bounds
    args.__setattr__('lower', -1)
    args.__setattr__('upper', 1)
    args.__setattr__('device', 'cuda' if ch.cuda.is_available() else 'cpu')

    print('args: ', args)

    args.__setattr__('phi', Left(Tensor([args.C])))

    # run experiment
    main(args)
