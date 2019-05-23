# -*- coding: UTF-8 -*-

import logging
import numpy as np
import os
import typing
from typing import Any, Dict, List, Optional, Text, Tuple

from rasa_nlu import utils
from rasa_nlu.classifiers import INTENT_RANKING_LENGTH
from rasa_nlu.components import Component
from rasa_nlu.config import RasaNLUModelConfig
from rasa_nlu.model import Metadata
from rasa_nlu.training_data import Message, TrainingData
from rasa_nlu.training_data import load_data
from rasa_nlu.tokenizers import Token, Tokenizer

logger = logging.getLogger(__name__)

if typing.TYPE_CHECKING:
    import sklearn

SKLEARN_MODEL_FILE_NAME = "my_intent_classifier_sklearn.pkl"


def _sklearn_numpy_warning_fix():
    """Fixes unecessary warnings emitted by sklearns use of numpy.

    Sklearn will fix the warnings in their next release in ~ August 2018.
    based on https://stackoverflow.com/a/49668081"""
    import warnings

    warnings.filterwarnings(module='sklearn*', action='ignore',
                            category=DeprecationWarning)


class MySklearnIntentClassifier(Component):
    """Intent classifier using the sklearn framework"""

    name = "my_intent_classifier_sklearn"

    provides = ["intent", "intent_ranking"]

    requires = ["text_features"]

    defaults = {
        # C parameter of the svm - cross validation will select the best value
        "C": [1, 2, 5, 10, 20, 100],

        # gamma parameter of the svm
        "gamma": [0.1],

        # the kernels to use for the svm training - cross validation will
        # decide which one of them performs best
        "kernels": ["linear"],

        # We try to find a good number of cross folds to use during
        # intent training, this specifies the max number of folds
        "max_cross_validation_folds": 5,

        # Scoring function used for evaluating the hyper parameters
        # This can be a name or a function (cfr GridSearchCV doc for more info)
        "scoring_function": "f1_weighted"
    }

    model_path = None

    def __init__(self,
                 component_config: Dict[Text, Any] = None,
                 clf: 'sklearn.model_selection.GridSearchCV' = None,
                 le: Optional['sklearn.preprocessing.LabelEncoder'] = None
                 ) -> None:
        """Construct a new intent classifier using the sklearn framework."""
        from sklearn.preprocessing import LabelEncoder

        super(MySklearnIntentClassifier, self).__init__(component_config)

        if le is not None:
            self.le = le
        else:
            self.le = LabelEncoder()
        self.clf = clf

        _sklearn_numpy_warning_fix()

    @classmethod
    def required_packages(cls) -> List[Text]:
        return ["sklearn"]

    def transform_labels_str2num(self, labels: List[Text]) -> np.ndarray:
        """Transforms a list of strings into numeric label representation.

        :param labels: List of labels to convert to numeric representation"""

        return self.le.fit_transform(labels)

    def transform_labels_num2str(self, y: np.ndarray) -> np.ndarray:
        """Transforms a list of strings into numeric label representation.

        :param y: List of labels to convert to numeric representation"""

        return self.le.inverse_transform(y)

    def train(self,
              training_data: TrainingData,
              cfg: RasaNLUModelConfig,
              **kwargs: Any) -> None:
        """Train the intent classifier on a data set."""

        num_threads = kwargs.get("num_threads", 1)

        labels = [e.get("intent")
                  for e in training_data.intent_examples]

        if len(set(labels)) < 2:
            logger.warning("Can not train an intent classifier. "
                           "Need at least 2 different classes. "
                           "Skipping training of intent classifier.")
        else:
            y = self.transform_labels_str2num(labels)
            X = np.stack([example.get("text_features")
                          for example in training_data.intent_examples])

            self.clf = self._create_classifier(num_threads, y)

            self.clf.fit(X, y)

    def _num_cv_splits(self, y):
        folds = self.component_config["max_cross_validation_folds"]
        return max(2, min(folds, np.min(np.bincount(y)) // 5))

    def _create_classifier(self, num_threads, y):
        from sklearn.model_selection import GridSearchCV
        from sklearn.svm import SVC

        C = self.component_config["C"]
        kernels = self.component_config["kernels"]
        gamma = self.component_config["gamma"]
        # dirty str fix because sklearn is expecting
        # str not instance of basestr...
        tuned_parameters = [{"C": C,
                             "gamma": gamma,
                             "kernel": [str(k) for k in kernels]}]

        # aim for 5 examples in each fold

        cv_splits = self._num_cv_splits(y)

        return GridSearchCV(SVC(C=1,
                                probability=True,
                                class_weight='balanced'),
                            param_grid=tuned_parameters,
                            n_jobs=num_threads,
                            cv=cv_splits,
                            scoring=self.component_config['scoring_function'],
                            verbose=1)

    def features_for_tokens(
            self,
            tokens: List[Token],
            feature_extractor: 'mitie.total_word_feature_extractor'
    ) -> np.ndarray:

        vec = np.zeros(self.ndim(feature_extractor))
        for token in tokens:
            vec += feature_extractor.get_feature_vector(token.text)
        if tokens:
            return vec / len(tokens)
        else:
            return vec

    def similarity(self, message: Message, **kwargs: Any):
        import jieba

        """
        1.加载训练数据
        2.获取词向量提取器
        3.计算距离训练数据与词向量距离
        4.返回最相似的词
        :return:
        """
        if not self.model_path:
            logger.error("the model_path is None!")
            return None

        resource_name = os.path.join(self.model_path, "training_data.json")
        language = "zh"
        # 1.加载训练数据
        training_data = load_data(resource_name, language)

        # 2.获取词向量提取器
        mitie_feature_extractor = kwargs.get("mitie_feature_extractor")
        if not mitie_feature_extractor:
            logger.error("the mitie_feature_extractor is None!")
            return None

        # 3.计算距离训练数据与词向量距离
        text = message.text
        tokenized = jieba.tokenize(text)
        tokens = [Token(word, start) for (word, start, end) in tokenized]
        v_text = self.features_for_tokens(tokens, mitie_feature_extractor)
        X = message.get("text_features")
        score = 0
        for example in training_data.intent_examples:
            example_text = example.text
            tokenized2 = jieba.tokenize(example_text)
            tokens2 = [Token(word, start) for (word, start, end) in tokenized2]
            v_example_text = self.features_for_tokens(tokens2, mitie_feature_extractor)
            c_score = np.dot(v_text, v_example_text) / (np.linalg.norm(v_text) * np.linalg.norm(v_example_text))
            if c_score > score:
                score = c_score
                X = v_example_text

        return X

    def process(self, message: Message, **kwargs: Any) -> None:
        """Return the most likely intent and its probability for a message."""

        if not self.clf:
            # component is either not trained or didn't
            # receive enough training data
            intent = None
            intent_ranking = []
        else:
            X = message.get("text_features").reshape(1, -1)
            intent_ids, probabilities = self.predict(X)
            intents = self.transform_labels_num2str(np.ravel(intent_ids))
            # `predict` returns a matrix as it is supposed
            # to work for multiple examples as well, hence we need to flatten
            probabilities = probabilities.flatten()

            X1 = self.similarity(message, **kwargs)
            if X1 :
                intent_ids1, probabilities1 = self.predict(X1.reshape(1, -1))
                intents1 = self.transform_labels_num2str(np.ravel(intent_ids1))
                # `predict` returns a matrix as it is supposed
                # to work for multiple examples as well, hence we need to flatten
                probabilities1 = probabilities1.flatten()
                if intents1.size > 0 and probabilities1.size > 0:
                    if intents.size > 0 and probabilities.size > 0:
                        if probabilities1[0] > probabilities[0]:
                            intent_ids = intent_ids1
                            intents = intents1
                            probabilities = probabilities1
                            logger.debug("use similarity text!")
                    else:
                        intent_ids = intent_ids1
                        intents = intents1
                        probabilities = probabilities1
                        logger.debug("use similarity text!")

            if intents.size > 0 and probabilities.size > 0:
                ranking = list(zip(list(intents),
                                   list(probabilities)))[:INTENT_RANKING_LENGTH]

                intent = {"name": intents[0], "confidence": probabilities[0]}

                intent_ranking = [{"name": intent_name, "confidence": score}
                                  for intent_name, score in ranking]
            else:
                intent = {"name": None, "confidence": 0.0}
                intent_ranking = []

        message.set("intent", intent, add_to_output=True)
        message.set("intent_ranking", intent_ranking, add_to_output=True)

    def predict_prob(self, X: np.ndarray) -> np.ndarray:
        """Given a bow vector of an input text, predict the intent label.

        Return probabilities for all labels.

        :param X: bow of input text
        :return: vector of probabilities containing one entry for each label"""

        return self.clf.predict_proba(X)

    def predict(self, X: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """Given a bow vector of an input text, predict most probable label.

        Return only the most likely label.

        :param X: bow of input text
        :return: tuple of first, the most probable label and second,
                 its probability."""

        pred_result = self.predict_prob(X)
        # sort the probabilities retrieving the indices of
        # the elements in sorted order
        sorted_indices = np.fliplr(np.argsort(pred_result, axis=1))
        return sorted_indices, pred_result[:, sorted_indices]

    @classmethod
    def load(cls,
             model_dir: Optional[Text] = None,
             model_metadata: Optional[Metadata] = None,
             cached_component: Optional['SklearnIntentClassifier'] = None,
             **kwargs: Any
             ) -> 'SklearnIntentClassifier':

        model_path = model_dir
        meta = model_metadata.for_component(cls.name)
        file_name = meta.get("classifier_file", SKLEARN_MODEL_FILE_NAME)
        classifier_file = os.path.join(model_dir, file_name)

        if os.path.exists(classifier_file):
            return utils.pycloud_unpickle(classifier_file)
        else:
            return cls(meta)

    def persist(self, model_dir: Text) -> Optional[Dict[Text, Any]]:
        """Persist this model into the passed directory."""

        classifier_file = os.path.join(model_dir, SKLEARN_MODEL_FILE_NAME)
        utils.pycloud_pickle(classifier_file, self)
        return {"classifier_file": SKLEARN_MODEL_FILE_NAME}
