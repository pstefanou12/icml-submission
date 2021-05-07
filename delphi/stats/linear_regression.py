"""
Truncated regression
"""

import torch as ch
from torch import Tensor
import torch.nn as nn
from torch.nn import Linear
from cox.utils import Parameters
import config

from .stats import stats
from ..oracle import oracle
from ..train import train_model
from ..grad import TruncatedMSE, TrunatedUnknownVarianceMSE
from ..utils.helpers import Bounds
from ..utils import defaults
from ..utils.datasets import DataSet, TRUNC_REG_OPTIONAL_ARGS, TRUNC_REG_REQUIRED_ARGS, TruncatedRegression


class LinearRegression(stats):
    """
    """
    def __init__(
            self,
            phi: oracle,
            alpha: float,
            args: Parameters,
            bias: bool=True,
            var: float = None,
            device: str="cpu",
            **kwargs):
        """
        """
        super(truncated_regression).__init__()
        # check algorithm hyperparameters
        config.args = defaults.check_and_fill_args(args, defaults.REGRESSION_ARGS, TruncatedRegression)
        # add oracle and survival prob to parameters
        config.args.__setattr__('phi', phi)
        config.args.__setattr__('alpha', alpha)
        config.args.__setattr__('device', device)
        config.args.__setattr__('bias', bias)
        config.args.__setattr__('var', var)
        config.args.__setattr__('score', True)

        self._lin_reg = None
        self.projection_set = None
        # intialize loss function and add custom criterion to hyperparameters
        if not config.args.var:
            self.criterion = TruncatedUnknownVarianceMSE.apply
        else:
            self.criterion = TruncatedMSE.apply
        self._emp_lin_reg = None
        config.args.__setattr__('custom_criterion', self.criterion)

    def fit(self, X: Tensor, y: Tensor):
        """
        """
        # create dataset and dataloader
        ds_kwargs = {
            'custom_class_args': {
                'X': X, 'y': y, 'bias': config.args.bias, 'unknown': True if not config.args.var else False},
            'custom_class': TruncatedRegression,
            'transform_train': None,
            'transform_test': None,
        }
        ds = DataSet('truncated_regression', TRUNC_REG_REQUIRED_ARGS, TRUNC_REG_OPTIONAL_ARGS, data_path=None,
                     **ds_kwargs)
        loaders = ds.make_loaders(workers=config.args.workers, batch_size=config.args.batch_size)
        # initialize model with empirical estimates
        if config.args.var:
            self._emp_lin_reg = Linear(in_features=loaders[0].dataset.w.size(0), out_features=1, bias=config.args.bias)
            self._emp_lin_reg.weight.data = loaders[0].dataset.w.t()
            self._emp_lin_reg.bias = ch.nn.Parameter(loaders[0].dataset.w0) if config.args.bias else None
            self.projection_set = TruncatedRegressionProjectionSet(self._emp_lin_reg)
            update_params = None
        else:
            self._emp_lin_reg = LinearUnknownVariance(loaders[0].dataset.v, loaders[0].dataset.lambda_,
                                                      bias=loaders[0].dataset.v0)
            self.projection_set = TruncatedUnknownVarianceProjectionSet(self._emp_lin_reg)
            update_params = [
                    {'params': self._emp_lin_reg.v},
                    {'params': self._emp_lin_reg.bias},
                    {'params': self._emp_lin_reg.lambda_, 'lr': config.args.var_lr}]

        config.args.__setattr__('iteration_hook', self.projection_set)
        # run PGD for parameter estimation
        return train_model(config.args, self._emp_lin_reg, loaders, update_params=update_params)


class TruncatedRegressionProjectionSet:
    """
    Project to domain for linear regression with known variance
    """
    def __init__(self, emp_lin_reg):
        self.emp_weight = emp_lin_reg.weight.data
        self.emp_bias = emp_lin_reg.bias.data if config.args.bias else None
        self.radius = config.args.radius * (4.0 * ch.log(2.0 / config.args.alpha) + 7.0)
        if config.args.clamp:
            self.weight_bounds = Bounds(self.emp_weight.flatten() - config.args.radius,
                                        self.emp_weight.flatten() + config.args.radius)
            self.bias_bounds = Bounds(self.emp_bias.flatten() - config.args.radius,
                                      self.emp_bias.flatten() + config.args.radius) if config.args.bias else None
        else:
            pass

    def __call__(self, M, i, loop_type, inp, target):
        if config.args.clamp:
            M.weight.data = ch.stack(
                [ch.clamp(M.weight[i], self.weight_bounds.lower[i], self.weight_bounds.upper[i]) for i in
                 range(M.weight.size(0))])
            if config.args.bias:
                M.bias.data = ch.clamp(M.bias, float(self.bias_bounds.lower), float(self.bias_bounds.upper)).reshape(
                    M.bias.size())
        else:
            pass


class TruncatedUnknownVarianceProjectionSet:
    """
    Project parameter estimation back into domain of expected results for censored normal distributions.
    """

    def __init__(self, emp_lin_reg):
        """
        :param emp_lin_reg: empirical regression with unknown noise variance
        """
        self.emp_var = emp_lin_reg.lambda_.data.inverse()
        self.emp_weight = emp_lin_reg.v.data * self.emp_var
        self.emp_bias = emp_lin_reg.bias.data * self.emp_var if config.args.bias else None
        self.param_radius = config.args.radius * (12.0 + 4.0 * ch.log(2.0 / config.args.alpha))

        if config.args.clamp:
            self.weight_bounds, self.var_bounds = Bounds(self.emp_weight.flatten() - self.param_radius,
                                                         self.emp_weight.flatten() + self.param_radius), Bounds(
                self.emp_var.flatten() / config.args.radius, (self.emp_var.flatten()) / config.args.alpha.pow(2))
            self.bias_bounds = Bounds(self.emp_bias.flatten() - self.param_radius,
                                      self.emp_bias.flatten() + self.param_radius) if config.args.bias else None
        else:
            pass

    def __call__(self, M, i, loop_type, inp, target):
        var = M.lambda_.inverse()
        weight = M.v.data * var

        if config.args.clamp:
            # project noise variance
            M.lambda_.data = ch.clamp(var, float(self.var_bounds.lower), float(self.var_bounds.upper)).inverse()
            # project weights
            M.v.data = ch.cat(
                [ch.clamp(weight[i].unsqueeze(0), float(self.weight_bounds.lower[i]),
                          float(self.weight_bounds.upper[i]))
                 for i in range(weight.size(0))]) * M.lambda_
            # project bias
            if config.args.bias:
                bias = M.bias * var
                M.bias.data = ch.clamp(bias, float(self.bias_bounds.lower), float(self.bias_bounds.upper)) * M.lambda_
        else:
            pass


class LinearUnknownVariance(nn.Module):
    """
    Linear layer with unknown noise variance.
    """
    def __init__(self, v, lambda_, bias=None):
        """
        :param lambda_: 1/empirical variance
        :param v: empirical weight*lambda_ estimate
        :param bias: (optional) empirical bias*lambda_ estimate
        """
        super(LinearUnknownVariance, self).__init__()
        self.register_parameter(name='v', param=ch.nn.Parameter(v))
        self.register_parameter(name='lambda_', param=ch.nn.Parameter(lambda_))
        self.register_parameter(name='bias', param=ch.nn.Parameter(bias))

    def forward(self, x):
        var = self.lambda_.clone().detach().inverse()
        w = self.v*var
        if self.bias.nelement() > 0:
            return x.matmul(w) + self.bias * var
        return x.matmul(w)
