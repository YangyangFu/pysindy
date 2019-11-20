import warnings
import abc

import numpy as np
from scipy.linalg import cho_factor, cho_solve
from sklearn.base import RegressorMixin
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import ridge_regression
from sklearn.linear_model import Lasso
from sklearn.linear_model import ElasticNet as SKElasticNet
from sklearn.linear_model.base import _rescale_data
from sklearn.linear_model.base import LinearModel
from sklearn.utils.validation import check_X_y

# from sindy.utils.base import debug
from sindy.utils.base import get_prox


class BaseOptimizer(LinearModel, RegressorMixin):
    def __init__(
        self,
        normalize=False,
        fit_intercept=False,
        copy_X=True
    ):
        self.fit_intercept = fit_intercept
        self.normalize = normalize
        self.copy_X = copy_X
        self.iters = 0
        self.coef_ = []
        self.ind_ = []

        self.history_ = []

    # Force subclasses to implement this
    @abc.abstractmethod
    def _reduce(self):
        """Carry out the bulk of the work of the fit function"""
        raise NotImplementedError

    def fit(self, x_, y, sample_weight=None, **reduce_kws):
        x_, y = check_X_y(
            x_,
            y,
            accept_sparse=[],
            y_numeric=True,
            multi_output=False
        )

        x, y, X_offset, y_offset, X_scale = self._preprocess_data(
            x_,
            y,
            fit_intercept=self.fit_intercept,
            normalize=self.normalize,
            copy=self.copy_X,
            sample_weight=sample_weight,
        )

        if sample_weight is not None:
            x, y = _rescale_data(x, y, sample_weight)

        self.iters = 0
        self.ind_ = np.ones(x.shape[1], dtype=bool)
        self.coef_ = np.linalg.lstsq(x, y, rcond=None)[0]  # initial guess
        self.history_.append(self.coef_)

        self._reduce(x, y, **reduce_kws)

        self._set_intercept(X_offset, y_offset, X_scale)
        return self

    @property
    def complexity(self):
        return (
            np.count_nonzero(self.coef_)
            + np.count_nonzero([abs(self.intercept_) >= self.threshold])
        )


class STLSQ(BaseOptimizer):
    def __init__(
        self,
        threshold=0.1,
        alpha=0.0,
        max_iter=20,
        ridge_kw=None,
        **kwargs
    ):
        super(STLSQ, self).__init__(**kwargs)

        if threshold < 0:
            raise ValueError('threshold cannot be negative')
        if alpha < 0:
            raise ValueError('alpha cannot be negative')
        if max_iter <= 0:
            raise ValueError('max_iter must be positive')

        self.threshold = threshold
        self.alpha = alpha
        self.max_iter = max_iter
        self.ridge_kw = ridge_kw

    def _sparse_coefficients(self, dim, ind, coef, threshold):
        c = np.zeros(dim)
        c[ind] = coef
        big_ind = np.abs(c) >= threshold
        c[~big_ind] = 0
        self.history_.append(c)
        return c, big_ind

    def _regress(self, x, y):
        kw = self.ridge_kw or {}
        coef = ridge_regression(x, y, self.alpha, **kw)
        self.iters += 1
        return coef

    def _no_change(self):
        this_coef = self.history_[-1]
        if len(self.history_) > 1:
            last_coef = self.history_[-2]
        else:
            last_coef = np.zeros_like(this_coef)
        return all(bool(i) == bool(j) for i, j in zip(this_coef, last_coef))

    def _reduce(self, x, y):
        """
        Iterates the thresholding. Assumes an initial guess is saved in
        self.coef_ and self.ind_
        """
        ind = self.ind_
        n_samples, n_features = x.shape
        n_features_selected = sum(ind)

        for _ in range(self.max_iter):
            if np.count_nonzero(ind) == 0:
                warnings.warn(
                    """Sparsity parameter is too big ({}) and eliminated all
                    coeficients""".format(self.threshold)
                )
                coef = np.zeros_like(ind, dtype=float)
                break

            coef = self._regress(x[:, ind], y)
            coef, ind = self._sparse_coefficients(
                n_features,
                ind,
                coef,
                self.threshold
            )

            if sum(ind) == n_features_selected or self._no_change():
                # could not (further) select important features
                break
        else:
            warnings.warn(
                "STLSQ._reduce did not converge after {} iterations.".format(
                    self.max_iter
                ),
                ConvergenceWarning,
            )
            try:
                coef
            except NameError:
                coef = self.coef_
                warnings.warn(
                    "STLSQ._reduce has no iterations left to determine coef",
                    ConvergenceWarning
                )
        self.coef_ = coef
        self.ind_ = ind

    # def _unbias(self, x, y):
    #     if np.any(self.ind_):
    #         coef = self._regress(x[:, self.ind_], y, 0)
    #         self.coef_, self.ind_ = self._sparse_coefficients(x.shape[1], self.ind_, coef, self.threshold)


class SR3(BaseOptimizer):
    def __init__(
        self,
        threshold=0.1,
        nu=1.0,
        tol=1e-5,
        thresholder='l0',
        max_iter=30,
        **kwargs
    ):
        super(SR3, self).__init__(**kwargs)

        if threshold < 0:
            raise ValueError('threshold cannot be negative')
        if nu <= 0:
            raise ValueError('nu must be positive')
        if tol <= 0:
            raise ValueError('tol must be positive')
        if max_iter <= 0:
            raise ValueError('max_iter must be positive')

        self.threshold = threshold
        self.nu = nu
        self.tol = tol
        self.thresholder = thresholder
        self.prox = get_prox(thresholder)
        self.max_iter = max_iter

    def _update_full_coef(self, cho, x_transpose_y, coef_sparse):
        b = x_transpose_y + coef_sparse / self.nu
        coef_full = cho_solve(cho, b)
        self.iters += 1
        return coef_full

    def _update_sparse_coef(self, coef_full):
        coef_sparse = self.prox(coef_full, self.threshold)
        self.history_.append(coef_sparse)
        return coef_sparse

    def _convergence_criterion(self):
        this_coef = self.history_[-1]
        if len(self.history_) > 1:
            last_coef = self.history_[-2]
        else:
            last_coef = np.zeros_like(this_coef)
        return np.sum((this_coef - last_coef)**2)

    def _reduce(self, x, y):
        """
        Iterates the thresholding. Assumes an initial guess
        is saved in self.coef_ and self.ind_
        """
        coef_sparse = self.coef_
        n_samples, n_features = x.shape

        # Precompute some objects for upcoming least-squares solves.
        # Assumes that self.nu is fixed throughout optimization procedure.
        cho = cho_factor(
            np.dot(x.T, x) + np.diag(np.full(x.shape[1], 1.0 / self.nu))
        )
        x_transpose_y = np.dot(x.T, y)

        for _ in range(self.max_iter):
            coef_full = self._update_full_coef(
                cho,
                x_transpose_y,
                coef_sparse
            )
            coef_sparse = self._update_sparse_coef(coef_full)

            if self._convergence_criterion() < self.tol:
                # Could not (further) select important features
                break
        else:
            warnings.warn(
                "SR3._reduce did not converge after {} iterations.".format(
                    self.max_iter
                ),
                ConvergenceWarning,
            )

        self.coef_ = coef_sparse
        self.coef_full_ = coef_full


class LASSO(BaseOptimizer):
    def __init__(
        self,
        alpha=1.0,
        lasso_kw=None,
        max_iter=1000,
        **kwargs
    ):
        super(LASSO, self).__init__(**kwargs)

        if alpha < 0:
            raise ValueError('alpha cannot be negative')
        if max_iter <= 0:
            raise ValueError('max_iter must be positive')

        self.lasso_kw = lasso_kw
        self.alpha = alpha
        self.max_iter = max_iter

    def _reduce(self, x, y):
        kw = self.lasso_kw or {}
        lasso_model = Lasso(
            alpha=self.alpha,
            max_iter=self.max_iter,
            fit_intercept=False,
            **kw
        )

        lasso_model.fit(x, y)

        self.coef_ = lasso_model.coef_
        self.iters = lasso_model.n_iter_


class ElasticNet(BaseOptimizer):
    def __init__(
        self,
        alpha=1.0,
        l1_ratio=0.5,
        max_iter=1000,
        elastic_net_kw={},
        **kwargs
    ):
        super(ElasticNet, self).__init__(**kwargs)

        if alpha < 0:
            raise ValueError('alpha must be nonnegative')
        if l1_ratio < 0:
            raise ValueError('l1_ratio must be nonnegative')
        if max_iter <= 0:
            raise ValueError('max_iter must be positive')

        self.alpha = alpha
        self.l1_ratio = l1_ratio
        self.max_iter = max_iter
        self.elastic_net_kw = elastic_net_kw

    def _reduce(self, x, y):
        kw = self.elastic_net_kw or {}
        elastic_net_model = SKElasticNet(
            alpha=self.alpha,
            l1_ratio=self.l1_ratio,
            max_iter=self.max_iter,
            fit_intercept=False,
            **kw
        )

        elastic_net_model.fit(x, y)

        self.coef_ = elastic_net_model.coef_
        self.iters = elastic_net_model.n_iter_
