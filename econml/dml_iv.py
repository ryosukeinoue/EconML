# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.

"""Double ML IV for Heterogeneous Treatment Effects.

An Double/Orthogonal machine learning approach to estimation of heterogeneous
treatment effect with an endogenous treatment and an instrument. It
implements the DMLIV algorithm from the paper:

Machine Learning Estimation of Heterogeneous Treatment Effects with Instruments
Vasilis Syrgkanis, Victor Lei, Miruna Oprescu, Maggie Hei, Keith Battocchi, Greg Lewis
https://arxiv.org/abs/1905.10176

"""

import numpy as np
from sklearn.base import clone
from sklearn.linear_model import LinearRegression
from ._ortho_learner import _OrthoLearner
from .dml import _FinalWrapper
from .utilities import (hstack, StatsModelsLinearRegression)
from .inference import StatsModelsInference
from .cate_estimator import StatsModelsCateEstimatorMixin


# A cut-down version of the DML first stage wrapper, since we don't need to support W or linear first stages


class _FirstStageWrapper:
    def __init__(self, model, discrete_target):
        self._model = clone(model, safe=False)
        self._discrete_target = discrete_target

    def _combine(self, X, Z, n_samples, fitting=True):
        if X is None:
            # if both X and Z are None, just return a column of ones
            return (Z if Z is not None else np.ones((n_samples, 1)))
        XZ = hstack([X, Z.reshape(n_samples, -1)]) if Z is not None else X
        return XZ

    def fit(self, X, *args, sample_weight=None):
        if len(args) == 1:
            Target, = args
            Z = None
        else:
            (Z, Target) = args
        if self._discrete_target:
            # In this case, the Target is the one-hot-encoding of the treatment variable
            # We need to go back to the label representation of the one-hot so as to call
            # the classifier.
            if np.any(np.all(Target == 0, axis=0)) or (not np.any(np.all(Target == 0, axis=1))):
                raise AttributeError("Provided crossfit folds contain training splits that " +
                                     "don't contain all treatments")
            Target = inverse_onehot(Target)

        if sample_weight is not None:
            self._model.fit(self._combine(X, Z, Target.shape[0]), Target, sample_weight=sample_weight)
        else:
            self._model.fit(self._combine(X, Z, Target.shape[0]), Target)

    def predict(self, X, Z=None):
        n_samples = X.shape[0] if X is not None else (Z.shape[0] if Z is not None else 1)
        if self._discrete_target:
            return self._model.predict_proba(self._combine(X, Z, n_samples, fitting=False))[:, 1:]
        else:
            return self._model.predict(self._combine(X, Z, n_samples, fitting=False))


class _BaseDMLATEIV(_OrthoLearner):
    def __init__(self, model_nuisance,
                 discrete_instrument=False, discrete_treatment=False,
                 n_splits=2, random_state=None):
        class ModelFinal:
            def __init__(self):
                self._first_stage = LinearRegression(fit_intercept=False)
                self._model_final = _FinalWrapper(LinearRegression(fit_intercept=False),
                                                  fit_cate_intercept=True, featurizer=None, use_weight_trick=False)

            def fit(self, Y, T, X=None, W=None, Z=None, nuisances=None, sample_weight=None, sample_var=None):
                Y_res, T_res, Z_res = nuisances
                # DMLATEIV is just like 2SLS; first regress T_res on Z_res, then regress Y_res on predicted T_res
                T_res_pred = self._first_stage.fit(Z_res, T_res,
                                                   sample_weight=sample_weight).predict(Z_res)
                # TODO: allow the final model to actually use X?
                self._model_final.fit(X=None, T=T_res_pred, Y=Y_res, sample_weight=sample_weight)
                return self

            def predict(self, X=None):
                # TODO: allow the final model to actually use X?
                return self._model_final.predict(X=None)

            def score(self, Y, T, X=None, W=None, Z=None, nuisances=None, sample_weight=None, sample_var=None):
                Y_res, T_res, Z_res = nuisances
                if Y_res.ndim == 1:
                    Y_res = Y_res.reshape((-1, 1))
                if T_res.ndim == 1:
                    T_res = T_res.reshape((-1, 1))
                # TODO: allow the final model to actually use X?
                effects = self._model_final.predict(X=None).reshape((-1, Y_res.shape[1], T_res.shape[1]))
                Y_res_pred = np.einsum('ijk,ik->ij', effects, T_res).reshape(Y_res.shape)
                if sample_weight is not None:
                    return np.mean(np.average((Y_res - Y_res_pred)**2, weights=sample_weight, axis=0))
                else:
                    return np.mean((Y_res - Y_res_pred) ** 2)

        super().__init__(model_nuisance, ModelFinal(),
                         discrete_treatment=discrete_treatment, discrete_instrument=discrete_instrument,
                         n_splits=n_splits, random_state=random_state)

    def fit(self, Y, T, Z, X=None, *, sample_weight=None, sample_var=None, inference=None):
        """
        Estimate the counterfactual model from data, i.e. estimates function :math:`\\theta(\\cdot)`.

        Parameters
        ----------
        Y: (n, d_y) matrix or vector of length n
            Outcomes for each sample
        T: (n, d_t) matrix or vector of length n
            Treatments for each sample
        Z: (n, d_z) matrix
            Instruments for each sample
        X: optional(n, d_x) matrix or None (Default=None)
            Features for each sample
        sample_weight: optional(n,) vector or None (Default=None)
            Weights for each samples
        sample_var: optional(n,) vector or None (Default=None)
            Sample variance for each sample
        inference: string,:class:`.Inference` instance, or None
            Method for performing inference.  This estimator supports 'bootstrap'
            (or an instance of:class:`.BootstrapInference`).

        Returns
        -------
        self: _BaseDMLATEIV instance
        """
        # Replacing fit from _OrthoLearner, to enforce W=None and improve the docstring
        return super().fit(Y, T, X=X, W=None, Z=Z,
                           sample_weight=sample_weight, sample_var=sample_var, inference=inference)

    def score(self, Y, T, Z, X=None):
        """
        Score the fitted CATE model on a new data set. Generates nuisance parameters
        for the new data set based on the fitted residual nuisance models created at fit time.
        It uses the mean prediction of the models fitted by the different crossfit folds.
        Then calculates the MSE of the final residual Y on residual T regression.

        If model_final does not have a score method, then it raises an :exc:`.AttributeError`

        Parameters
        ----------
        Y: (n, d_y) matrix or vector of length n
            Outcomes for each sample
        T: (n, d_t) matrix or vector of length n
            Treatments for each sample
        Z: optional(n, d_z) matrix
            Instruments for each sample
        X: optional(n, d_x) matrix or None (Default=None)
            Features for each sample


        Returns
        -------
        score: float
            The MSE of the final CATE model on the new data.
        """
        # Replacing score from _OrthoLearner, to enforce W=None and improve the docstring
        return super().score(Y, T, X=X, Z=Z)


class DMLATEIV(_BaseDMLATEIV):
    def __init__(self, model_Y_X, model_T_X, model_Z_X,
                 discrete_treatment=False, discrete_instrument=False,
                 n_splits=2, random_state=None):
        class ModelNuisance:
            def __init__(model_Y_X, model_T_X, model_Z_X):
                self._model_Y_X = clone(model_Y_X, safe=False)
                self._model_T_X = clone(model_T_X, safe=False)
                self._model_Z_X = clone(model_Z_X, safe=False)

            def fit(self, Y, T, X=None, W=None, Z=None, sample_weight=None):
                assert W is None, "DML ATE IV does not accept controls"
                self._model_Y_X.fit(X, Y, sample_weight=sample_weight)
                self._model_T_X.fit(X, T, sample_weight=sample_weight)
                self._model_Z_X.fit(X, Z, sample_weight=sample_weight)
                return self

            def predict(self, Y, T, X=None, W=None, Z=None, sample_weight=None):
                Y_pred = self._model_Y_X.predict(X)
                T_pred = self._model_T_X.predict(X)
                Z_pred = self._model_Z_X.predict(X)
                if X is None:  # In this case predict above returns a single row
                    Y_pred = np.tile(Y_pred.reshape(1, -1), (Y.shape[0], 1))
                    T_pred = np.tile(T_pred.reshape(1, -1), (T.shape[0], 1))
                    Z_pred = np.tile(Z_pred.reshape(1, -1), (Z.shape[0], 1))
                Y_res = Y - Y_pred.reshape(Y.shape)
                T_res = T - T_pred.reshape(T.shape)
                Z_res = Z - Z_pred.reshape(Z.shape)
                return Y_res, T_res


class ProjectDMLATEIV(_BaseDMLATEIV):
    def __init__(self, model_Y_X, model_T_X, model_T_XZ,
                 discrete_treatment=False, discrete_instrument=False,
                 n_splits=2, random_state=None):
        class ModelNuisance:
            def __init__(model_Y_X, model_T_X, model_T_XZ):
                self._model_Y_X = clone(model_Y_X, safe=False)
                self._model_T_X = clone(model_T_X, safe=False)
                self._model_T_XZ = clone(model_T_XZ, safe=False)

            def fit(self, Y, T, X=None, W=None, Z=None, sample_weight=None):
                assert W is None, "DML ATE IV does not accept controls"
                self._model_Y_X.fit(X, Y, sample_weight=sample_weight)
                self._model_T_X.fit(X, T, sample_weight=sample_weight)
                self._model_T_XZ.fit(X, Z, T, sample_weight=sample_weight)
                return self

            def predict(self, Y, T, X=None, W=None, Z=None, sample_weight=None):
                Y_pred = self._model_Y_X.predict(X)
                TX_pred = self._model_T_X.predict(X)
                TXZ_pred = self._model_T_XZ.predict(X)
                if X is None:  # In this case predict above returns a single row
                    Y_pred = np.tile(Y_pred.reshape(1, -1), (Y.shape[0], 1))
                    TXZ_pred = np.tile(TXZ_pred.reshape(1, -1), (T.shape[0], 1))
                    TX_pred = np.tile(TX_pred.reshape(1, -1), (T.shape[0], 1))
                Y_res = Y - Y_pred.reshape(Y.shape)
                T_res = T - TX_pred.reshape(T.shape)
                Z_res = TXZ_pred.reshape(T.shape) - TX_pred.reshape(T.shape)
                return Y_res, T_res, Z_res

        super().__init__(ModelNuisance(model_Y_X, model_T_X, model_T_XZ),
                         discrete_treatment=discrete_treatment, discrete_instrument=discrete_instrument,
                         n_splits=n_splits, random_state=random_state)


class _BaseDMLIV(_OrthoLearner):
    """
    The class _BaseDMLIV implements the base class of the DMLIV
    algorithm for estimating a CATE. It accepts three generic machine
    learning models:
    1) model_Y_X that estimates E[Y | X]
    2) model_T_X that estimates E[T | X]
    3) model_T_XZ that estimates E[T | X, Z]
    These are estimated in a cross-fitting manner for each sample in the training set.
    Then it minimizes the square loss:
    \sum_i (Y_i - E[Y|X_i] - theta(X) * (E[T|X_i, Z_i] - E[T|X_i]))^2
    This loss is minimized by the model_final class, which is passed as an input.
    In the two children classes {DMLIV, GenericDMLIV}, we implement different strategies of how to invoke
    machine learning algorithms to minimize this final square loss.


    Parameters
    ----------
    model_Y_X : estimator
        model to predict E[Y | X]

    model_T_X : estimator
        model to predict E[T | X]

    model_T_XZ : estimator
        model to predict E[T | X, Z]

    model_final : estimator
        final model that at fit time takes as input (Y-E[Y|X]), (E[T|X,Z]-E[T|X]) and X
        and supports method .effect(X) that produces the cate at X

    discrete_instrument: bool, optional, default False
        Whether the instrument values should be treated as categorical, rather than continuous, quantities

    discrete_treatment: bool, optional, default False
        Whether the treatment values should be treated as categorical, rather than continuous, quantities

    n_splits: int, cross-validation generator or an iterable, optional, default 2
        Determines the cross-validation splitting strategy.
        Possible inputs for cv are:

        - None, to use the default 3-fold cross-validation,
        - integer, to specify the number of folds.
        - :term:`CV splitter`
        - An iterable yielding (train, test) splits as arrays of indices.

        For integer/None inputs, if the treatment is discrete
        :class:`~sklearn.model_selection.StratifiedKFold` is used, else,
        :class:`~sklearn.model_selection.KFold` is used
        (with a random shuffle in either case).

        Unless an iterable is used, we call `split(concat[W, X], T)` to generate the splits. If all
        W, X are None, then we call `split(ones((T.shape[0], 1)), T)`.

    random_state: int, :class:`~numpy.random.mtrand.RandomState` instance or None, optional (default=None)
        If int, random_state is the seed used by the random number generator;
        If :class:`~numpy.random.mtrand.RandomState` instance, random_state is the random number generator;
        If None, the random number generator is the :class:`~numpy.random.mtrand.RandomState` instance used
        by :mod:`np.random<numpy.random>`.
    """

    def __init__(self, model_Y_X, model_T_X, model_T_XZ, model_final,
                 discrete_instrument=False, discrete_treatment=False, n_splits=2, random_state=None):
        class ModelNuisance:
            """
            Nuisance model fits the three models at fit time and at predict time
            returns Y-E[Y|X] and E[T|X,Z]-E[T|X] as residuals.
            """

            def __init__(self, model_Y_X, model_T_X, model_T_XZ):
                self._model_Y_X = clone(model_Y_X, safe=False)
                self._model_T_X = clone(model_T_X, safe=False)
                self._model_T_XZ = clone(model_T_XZ, safe=False)

            def fit(self, Y, T, X=None, W=None, Z=None, sample_weight=None):
                # TODO: would it be useful to extend to handle controls ala vanilla DML?
                assert W is None, "DML IV does not accept controls"
                self._model_Y_X.fit(X, Y, sample_weight=sample_weight)
                self._model_T_X.fit(X, T, sample_weight=sample_weight)
                self._model_T_XZ.fit(X, Z, T, sample_weight=sample_weight)
                return self

            def predict(self, Y, T, X=None, W=None, Z=None, sample_weight=None):
                Y_pred = self._model_Y_X.predict(X)
                TXZ_pred = self._model_T_XZ.predict(X, Z)
                TX_pred = self._model_T_X.predict(X)
                if X is None:  # In this case predict above returns a single row
                    Y_pred = np.tile(Y_pred.reshape(1, -1), (Y.shape[0], 1))
                    TXZ_pred = np.tile(TXZ_pred.reshape(1, -1), (T.shape[0], 1))
                    TX_pred = np.tile(TX_pred.reshape(1, -1), (T.shape[0], 1))
                Y_res = Y - Y_pred.reshape(Y.shape)
                T_res = TXZ_pred.reshape(T.shape) - TX_pred.reshape(T.shape)
                return Y_res, T_res

        class ModelFinal:
            """
            Final model at fit time, fits a residual on residual regression with a heterogeneous coefficient
            that depends on X, i.e.

                .. math ::
                    Y - E[Y | X] = \\theta(X) \\cdot (E[T | X, Z] - E[T | X]) + \\epsilon

            and at predict time returns :math:`\\theta(X)`. The score method returns the MSE of this final
            residual on residual regression.
            """

            def __init__(self, model_final):
                self._model_final = clone(model_final, safe=False)

            def fit(self, Y, T, X=None, W=None, Z=None, nuisances=None, sample_weight=None, sample_var=None):
                Y_res, T_res = nuisances
                self._model_final.fit(X, T_res, Y_res, sample_weight=sample_weight, sample_var=sample_var)
                return self

            def predict(self, X=None):
                return self._model_final.predict(X)

            def score(self, Y, T, X=None, W=None, Z=None, nuisances=None, sample_weight=None, sample_var=None):
                Y_res, T_res = nuisances
                if Y_res.ndim == 1:
                    Y_res = Y_res.reshape((-1, 1))
                if T_res.ndim == 1:
                    T_res = T_res.reshape((-1, 1))
                effects = self._model_final.predict(X).reshape((-1, Y_res.shape[1], T_res.shape[1]))
                Y_res_pred = np.einsum('ijk,ik->ij', effects, T_res).reshape(Y_res.shape)
                if sample_weight is not None:
                    return np.mean(np.average((Y_res - Y_res_pred)**2, weights=sample_weight, axis=0))
                else:
                    return np.mean((Y_res - Y_res_pred)**2)

        super().__init__(ModelNuisance(model_Y_X, model_T_X, model_T_XZ), ModelFinal(model_final),
                         discrete_treatment=discrete_treatment, discrete_instrument=discrete_instrument,
                         n_splits=n_splits, random_state=random_state)

    def fit(self, Y, T, Z, X=None, *, sample_weight=None, sample_var=None, inference=None):
        """
        Estimate the counterfactual model from data, i.e. estimates function :math:`\\theta(\\cdot)`.

        Parameters
        ----------
        Y: (n, d_y) matrix or vector of length n
            Outcomes for each sample
        T: (n, d_t) matrix or vector of length n
            Treatments for each sample
        Z: (n, d_z) matrix
            Instruments for each sample
        X: optional(n, d_x) matrix or None (Default=None)
            Features for each sample
        sample_weight: optional(n,) vector or None (Default=None)
            Weights for each samples
        sample_var: optional(n,) vector or None (Default=None)
            Sample variance for each sample
        inference: string,:class:`.Inference` instance, or None
            Method for performing inference.  This estimator supports 'bootstrap'
            (or an instance of:class:`.BootstrapInference`).

        Returns
        -------
        self: _BaseDMLIV
        """
        # Replacing fit from _OrthoLearner, to enforce W=None and improve the docstring
        return super().fit(Y, T, X=X, W=None, Z=Z,
                           sample_weight=sample_weight, sample_var=sample_var, inference=inference)

    def score(self, Y, T, Z, X=None):
        """
        Score the fitted CATE model on a new data set. Generates nuisance parameters
        for the new data set based on the fitted residual nuisance models created at fit time.
        It uses the mean prediction of the models fitted by the different crossfit folds.
        Then calculates the MSE of the final residual Y on residual T regression.

        If model_final does not have a score method, then it raises an :exc:`.AttributeError`

        Parameters
        ----------
        Y: (n, d_y) matrix or vector of length n
            Outcomes for each sample
        T: (n, d_t) matrix or vector of length n
            Treatments for each sample
        Z: optional(n, d_z) matrix
            Instruments for each sample
        X: optional(n, d_x) matrix or None (Default=None)
            Features for each sample


        Returns
        -------
        score: float
            The MSE of the final CATE model on the new data.
        """
        # Replacing score from _OrthoLearner, to enforce W=None and improve the docstring
        return super().score(Y, T, X=X, Z=Z)

    @property
    def original_featurizer(self):
        return super().model_final._model_final._original_featurizer

    @property
    def featurizer(self):
        # NOTE This is used by the inference methods and has to be the overall featurizer. intended
        # for internal use by the library
        return super().model_final._model_final._featurizer

    @property
    def model_final(self):
        # NOTE This is used by the inference methods and is more for internal use to the library
        return super().model_final._model_final._model

    @property
    def model_cate(self):
        """
        Get the fitted final CATE model.

        Returns
        -------
        model_cate: object of type(model_final)
            An instance of the model_final object that was fitted after calling fit which corresponds
            to the constant marginal CATE model.
        """
        return super().model_final._model_final._model

    @property
    def models_Y_X(self):
        """
        Get the fitted models for E[Y | X].

        Returns
        -------
        models_Y_X: list of objects of type(`model_Y_X`)
            A list of instances of the `model_Y_X` object. Each element corresponds to a crossfitting
            fold and is the model instance that was fitted for that training fold.
        """
        return [mdl._model for mdl in super().models_Y_X]

    @property
    def models_T_X(self):
        """
        Get the fitted models for E[T | X].

        Returns
        -------
        models_T_X: list of objects of type(`model_T_X`)
            A list of instances of the `model_T_X` object. Each element corresponds to a crossfitting
            fold and is the model instance that was fitted for that training fold.
        """
        return [mdl._model for mdl in super().models_T_X]

    @property
    def models_T_XZ(self):
        """
        Get the fitted models for E[T | X, Z].

        Returns
        -------
        models_T_XZ: list of objects of type(`model_T_XZ`)
            A list of instances of the `model_T_XZ` object. Each element corresponds to a crossfitting
            fold and is the model instance that was fitted for that training fold.
        """
        return [mdl._model for mdl in super().models_T_XZ]

    def cate_feature_names(self, input_feature_names=None):
        """
        Get the output feature names.

        Parameters
        ----------
        input_feature_names: list of strings of length X.shape[1] or None
            The names of the input features

        Returns
        -------
        out_feature_names: list of strings or None
            The names of the output features :math:`\\phi(X)`, i.e. the features with respect to which the
            final constant marginal CATE model is linear. It is the names of the features that are associated
            with each entry of the :meth:`coef_` parameter. Not available when the featurizer is not None and
            does not have a method: `get_feature_names(input_feature_names)`. Otherwise None is returned.
        """
        if self.original_featurizer is None:
            return input_feature_names
        elif hasattr(self.original_featurizer, 'get_feature_names'):
            return self.original_featurizer.get_feature_names(input_feature_names)
        else:
            raise AttributeError("Featurizer does not have a method: get_feature_names!")


class DMLIV(_BaseDMLIV):
    """
    A child of the _BaseDMLIV class that specifies a particular effect model
    where the treatment effect is linear in some featurization of the variable X
    The features are created by a provided featurizer that supports fit_transform.
    Then an arbitrary model fits on the composite set of features.

    Concretely, it assumes that theta(X)=<theta, phi(X)> for some features phi(X)
    and runs a linear model regression of Y-E[Y|X] on phi(X)*(E[T|X,Z]-E[T|X]).
    The features are created by the featurizer provided by the user. The particular
    linear model regression is also specified by the user (e.g. Lasso, ElasticNet)
    """

    def __init__(self, model_Y_X, model_T_X, model_T_XZ, model_final, featurizer=None,
                 fit_cate_intercept=True,
                 n_splits=2, discrete_instrument=False, discrete_treatment=False, random_state=None):
        """
        Parameters
        ----------
        model_Y_X : model to predict E[Y | X]
        model_T_X : model to predict E[T | X]
        model_T_XZ : model to predict E[T | X, Z]
        model_final : final linear model for predicting (Y-E[Y|X]) from phi(X) * (E[T|X,Z]-E[T|X])
            Method is incorrect if this model is not linear (e.g. Lasso, ElasticNet, LinearRegression).
        featurizer : object that creates features of X to use for effect model. Must have a method
            fit_transform that is applied on X to create phi(X).
        n_splits : number of splits to use in cross-fitting
        discrete_instrument : bool
            Whether to stratify cross-fitting splits by instrument
        discrete_treatment : bool
            whether to stratify cross-fitting splits by treatment
        """
        self.bias_part_of_coef = fit_cate_intercept
        self.fit_cate_intercept = fit_cate_intercept
        super().__init__(_FirstStageWrapper(model_Y_X, False),
                         _FirstStageWrapper(model_T_X, discrete_treatment),
                         _FirstStageWrapper(model_T_XZ, discrete_treatment),
                         _FinalWrapper(model_final,
                                       fit_cate_intercept=fit_cate_intercept,
                                       featurizer=featurizer,
                                       use_weight_trick=False),
                         n_splits=n_splits,
                         discrete_instrument=discrete_instrument,
                         discrete_treatment=discrete_treatment,
                         random_state=random_state)


class NonParamDMLIV(_BaseDMLIV):
    """
    A child of the _BaseDMLIV class that allows for an arbitrary square loss based ML
    method in the final stage of the DMLIV algorithm. The method has to support
    sample weights and the fit method has to take as input sample_weights (e.g. random forests), i.e.
    fit(X, y, sample_weight=None)
    It achieves this by re-writing the final stage square loss of the DMLIV algorithm as:
        \sum_i (E[T|X_i, Z_i] - E[T|X_i])^2 * ((Y_i - E[Y|X_i])/(E[T|X_i, Z_i] - E[T|X_i]) - theta(X))^2
    Then this can be viewed as a weighted square loss regression, where the target label is
        \tilde{Y}_i = (Y_i - E[Y|X_i])/(E[T|X_i, Z_i] - E[T|X_i])
    and each sample has a weight of
        V(X_i) = (E[T|X_i, Z_i] - E[T|X_i])^2
    Thus we can call any regression model with inputs:
        fit(X, \tilde{Y}_i, sample_weight=V(X_i))
    """

    def __init__(self, model_Y_X, model_T_X, model_T_XZ, model_final,
                 featurizer=None, fit_cate_intercept=True,
                 n_splits=2, discrete_instrument=False, discrete_treatment=False):
        """
        Parameters
        ----------
        model_Y_X : model to predict E[Y | X]
        model_T_X : model to predict E[T | X]
        model_T_XZ : model to predict E[T | X, Z]
        model_final : final model for predicting \tilde{Y} from X with sample weights V(X)
        n_splits : number of splits to use in cross-fitting
        discrete_instrument : whether to stratify cross-fitting splits by instrument
        discrete_treatment : whether to stratify cross-fitting splits by treatment
        """
        super().__init__(_FirstStageWrapper(model_Y_X, False),
                         _FirstStageWrapper(model_T_X, discrete_treatment),
                         _FirstStageWrapper(model_T_XZ, discrete_treatment),
                         _FinalWrapper(model_final, fit_cate_intercept, featurizer, True),
                         n_splits=n_splits,
                         discrete_instrument=discrete_instrument,
                         discrete_treatment=discrete_treatment)


class _BaseDRIV(_OrthoLearner):

    """
    The _BaseDRIV algorithm for estimating CATE with IVs. It is the parent of the
    two public classes {DRIV, ProjectedDRIV}

    Parameters
    ----------
    nuisance_models : dictionary of nuisance models, with {'name_of_model' : EstimatorObject, ...}

    model_final : estimator
        final model that at fit time takes as input (Y-E[Y|X]), (E[T|X,Z]-E[T|X]) and X
        and supports method .effect(X) that produces the cate at X

    cov_clip : float, optional, default 0.1
        clipping of the covariate for regions with low "overlap", to reduce variance

    opt_reweighted : bool, optional, default False
        Whether to reweight the samples to minimize variance. If True then
        model_final.fit must accept sample_weight as a kw argument. If True then
        assumes the model_final is flexible enough to fit the true CATE model. Otherwise,
        it method will return a biased projection to the model_final space, biased
        to give more weight on parts of the feature space where the instrument is strong.

    discrete_instrument: bool, optional, default False
        Whether the instrument values should be treated as categorical, rather than continuous, quantities

    discrete_treatment: bool, optional, default False
        Whether the treatment values should be treated as categorical, rather than continuous, quantities

    n_splits: int, cross-validation generator or an iterable, optional, default 2
        Determines the cross-validation splitting strategy.
        Possible inputs for cv are:

        - None, to use the default 3-fold cross-validation,
        - integer, to specify the number of folds.
        - :term:`CV splitter`
        - An iterable yielding (train, test) splits as arrays of indices.

        For integer/None inputs, if the treatment is discrete
        :class:`~sklearn.model_selection.StratifiedKFold` is used, else,
        :class:`~sklearn.model_selection.KFold` is used
        (with a random shuffle in either case).

        Unless an iterable is used, we call `split(concat[W, X], T)` to generate the splits. If all
        W, X are None, then we call `split(ones((T.shape[0], 1)), T)`.

    random_state: int, :class:`~numpy.random.mtrand.RandomState` instance or None, optional (default=None)
        If int, random_state is the seed used by the random number generator;
        If :class:`~numpy.random.mtrand.RandomState` instance, random_state is the random number generator;
        If None, the random number generator is the :class:`~numpy.random.mtrand.RandomState` instance used
        by :mod:`np.random<numpy.random>`.
    """

    def __init__(self,
                 nuisance_models,
                 model_final,
                 cov_clip=0.1, opt_reweighted=False,
                 discrete_instrument=False, discrete_treatment=False,
                 n_splits=2, random_state=None):
        class ModelFinal:
            """
            Final model at fit time, fits a residual on residual regression with a heterogeneous coefficient
            that depends on X, i.e.

                .. math ::
                    Y - E[Y | X] = \\theta(X) \\cdot (E[T | X, Z] - E[T | X]) + \\epsilon

            and at predict time returns :math:`\\theta(X)`. The score method returns the MSE of this final
            residual on residual regression.
            """

            def __init__(self, model_final):
                self._model_final = clone(model_final, safe=False)

            @staticmethod
            def _effect_estimate(nuisances):
                prel_theta, res_t, res_y, res_z, cov = nuisances

                # Estimate final model of theta(X) by minimizing the square loss:
                # (prel_theta(X) + (Y_res - prel_theta(X) * T_res) * Z_res / cov[T,Z | X] - theta(X))^2
                # We clip the covariance so that it is bounded away from zero, so as to reduce variance
                # at the expense of some small bias. For points with very small covariance we revert
                # to the model-based preliminary estimate and do not add the correction term.
                cov_sign = np.sign(cov)
                cov_sign[cov_sign == 0] = 1
                clipped_cov = cov_sign * np.clip(np.abs(cov),
                                                 self.cov_clip, np.inf)
                return prel_theta + (res_y - prel_theta * res_t) * res_z / clipped_cov

            def fit(self, Y, T, X=None, W=None, Z=None, nuisances=None, sample_weight=None, sample_var=None):
                theta_dr = self._effect_estimate(nuisances)

                # TODO: how do we incorporate the sample_weight and sample_var passed into this method
                #       as arguments?
                if self.opt_reweighted:
                    self._model_final.fit(X, theta_dr, sample_weight=clipped_cov**2)
                else:
                    self._model_final.fit(X, theta_dr)
                return self

            def predict(self, X=None):
                return self._model_final.predict(X)

            def score(self, Y, T, X=None, W=None, Z=None, nuisances=None, sample_weight=None, sample_var=None):
                # TODO: is there a good way to incorporate the other nuisance terms in the score?
                _, T_res, Y_res, _, _ = nuisances

                if Y_res.ndim == 1:
                    Y_res = Y_res.reshape((-1, 1))
                if T_res.ndim == 1:
                    T_res = T_res.reshape((-1, 1))
                effects = self._model_final.predict(X).reshape((-1, Y_res.shape[1], T_res.shape[1]))
                Y_res_pred = np.einsum('ijk,ik->ij', effects, T_res).reshape(Y_res.shape)

                if sample_weight is not None:
                    return np.mean(np.average((Y_res - Y_res_pred)**2, weights=sample_weight, axis=0))
                else:
                    return np.mean((Y_res - Y_res_pred)**2)
        self.cov_clip = cov_clip
        self.opt_reweighted = opt_reweighted
        super().__init__(model_nuisance, model_final,
                         discrete_instrument=discrete_instrument, discrete_treatment=discrete_treatment,
                         n_splits=n_splits, random_state=random_state)

    def fit(self, Y, T, Z, X=None, *, sample_weight=None, sample_var=None, inference=None):
        """
        Estimate the counterfactual model from data, i.e. estimates function :math:`\\theta(\\cdot)`.

        Parameters
        ----------
        Y: (n, d_y) matrix or vector of length n
            Outcomes for each sample
        T: (n, d_t) matrix or vector of length n
            Treatments for each sample
        Z: (n, d_z) matrix
            Instruments for each sample
        X: optional(n, d_x) matrix or None (Default=None)
            Features for each sample
        sample_weight: optional(n,) vector or None (Default=None)
            Weights for each samples
        sample_var: optional(n,) vector or None (Default=None)
            Sample variance for each sample
        inference: string,:class:`.Inference` instance, or None
            Method for performing inference.  This estimator supports 'bootstrap'
            (or an instance of:class:`.BootstrapInference`).

        Returns
        -------
        self: _BaseDRIV instance
        """
        # Replacing fit from _OrthoLearner, to enforce W=None and improve the docstring
        return super().fit(Y, T, X=X, W=None, Z=Z,
                           sample_weight=sample_weight, sample_var=sample_var, inference=inference)
