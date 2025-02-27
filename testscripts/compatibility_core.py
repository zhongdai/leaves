import abc
from collections import namedtuple
from datetime import datetime
from enum import Enum
import logging
import numpy as np
import os
import shutil
import subprocess
import sys
import tempfile
import venv

from util import dir_changer, execute_wrapper


class LibraryType(Enum):
    """
    Every test case has dependence on one of `LibraryType`. When virtual
    environment is setting up, the requirements for particular `LibraryType`
    are installed.
    """
    XGBOOST = 1
    LIGHTGBM = 2
    SKLEARN = 3


VEnv = namedtuple('VEnv', [
    'env_dir',
    'python_path',
    'pip_path',
    'env_name',
    'library',
    'version',
])


class VirtualEnvBuilder:
    """
    VirtualEnvBuilder is responsible to create or reuse python virtual
    envornments for particular `LibraryType` and `version`. Major method is
    `activate` which returns `VEnv` structure.
    """

    def __init__(self, root_dir: str, reuse_envs: bool):
        self.root_dir = os.path.abspath(root_dir)
        self.reuse_envs = reuse_envs
        self.logger = logging.getLogger('VirtualEnvBuilder')

    def activate(self, library_type: LibraryType, version: str):
        self.logger.info(f'Activating environment: {library_type.name} {version}')
        env_full_path = self._env_full_path(library_type, version)
        env = VEnv(
            env_dir=env_full_path,
            python_path=os.path.join(env_full_path, 'bin', 'python'),
            pip_path=os.path.join(env_full_path, 'bin', 'pip'),
            env_name=self._env_name(library_type, version),
            library=library_type,
            version=version,
        )
        if self._if_exist(library_type, version) and self.reuse_envs:
            self.logger.info('Use already existed environment')
            return env

        self.logger.info('Create new environment..')
        venv.create(
            env_dir=env_full_path,
            clear=True,
            symlinks=True,
            with_pip=True,
        )

        if library_type == LibraryType.LIGHTGBM:
            self.logger.info(f'Installing sklearn..')
            execute_wrapper([env.pip_path, 'install', 'sklearn'])
            lightgbm_package = f'lightgbm=={version}'
            self.logger.info(f'Installing {lightgbm_package}..')
            execute_wrapper([env.pip_path, 'install', lightgbm_package])
        else:
            self.logger.info(f'Installing sklearn..')
            execute_wrapper([env.pip_path, 'install', 'sklearn'])
            xgboost_package = f'xgboost=={version}'
            self.logger.info(f'Installing {xgboost_package}..')
            execute_wrapper([env.pip_path, 'install', xgboost_package])

        return env

    def _env_name(self, library_type, version):
        return f'{library_type.name.lower()}_{version}'

    def _env_full_path(self, library_type, version):
        return os.path.join(self.root_dir, self._env_name(library_type, version))

    def _if_exist(self, library_type, version):
        env_full_path = self._env_full_path(library_type, version)
        if os.path.exists(env_full_path):
            if not os.path.isdir(env_full_path):
                raise RuntimeError(f"'{env_full_path}' should be directory")
            return True
        return False


class CompareError(RuntimeError):
    pass


class CaseRunner:
    """
    CaseRunner is responsible for running particular test cases with
    different versions of libraries. After all cases the report can be
    generated by `report` method.
    """

    Outcome = namedtuple('Outcome', [
        'env',
        'case',
        'is_success',
        'reason',
    ])

    def __init__(self, env_builder: VirtualEnvBuilder, logger, leaves_path=None):
        self.env_builder = env_builder
        self.logger = logger
        self.leaves_path = leaves_path
        self.outcomes = []

    def run(self, case_class: type, dirname=None):
        """run test case on all environment versions"""
        for version in case_class.versions:
            self.run_single(case_class, version, dirname)

    def run_single(self, case_class: type, version: str, dirname=None):
        """run test case on the particular environment version from `version` parameter"""
        env = self.env_builder.activate(case_class.library, version)
        case = case_class(env, self.logger, dirname, self.leaves_path)
        is_success, reason = case.run(env)
        outcome = self.Outcome(
            env=env,
            case=case_class.__name__,
            is_success=is_success,
            reason=reason,
        )
        self.outcomes.append(outcome)


class ReportFormatter:
    def __init__(self, outcomes: list):
        self.outcomes = outcomes

    @staticmethod
    def head_text():
        return """
This file is autogenerated by [compatibility_test.py](testscripts/compatibility_test.py)
"""

    @staticmethod
    def tail_text():
        return """

## Details

X - not passed, V - passed

Generated {}
""".format(datetime.now().strftime('%Y-%m-%d %H:%M'))

    @staticmethod
    def _markdown_table(list_of_rows):
        """
        Generate markdown table by `list_of_rows`

        Example:
        > print(_markdown_table([['a', 'gggg'], ['abc', 'X'], ['c', 'V']]))

        Output:
            | a |gggg|
            |---|----|
            |abc| X  |
            | c | V  |
        """
        nrows = len(list_of_rows)
        if nrows == 0:
            return '\n'
        ncols = len(list_of_rows[0])

        fields_len = [0] * ncols
        for row in list_of_rows:
            for j in range(ncols):
                if fields_len[j] < len(row[j]):
                    fields_len[j] = len(row[j])

        format_string = '|'
        for width in fields_len:
            format_string += '{{: ^{}}}|'.format(width)
        format_string += '\n'

        outline_list = ['-'*width for width in fields_len]

        ret = ''
        ret += format_string.format(*list_of_rows[0])
        ret += format_string.format(*outline_list)
        for row in list_of_rows[1:]:
            ret += format_string.format(*row)

        return ret


    def report(self) -> str:
        """generate whole report about test cases outcomes"""
        ret = self.head_text()

        libraries = list(set(outcome.env.library for outcome in self.outcomes))

        for library in libraries:
            ret += f'\n## {library.name}\n\n'
            cases = list(sorted(set(outcome.case
                for outcome in self.outcomes
                if outcome.env.library == library
            )))
            versions = list(sorted(set(outcome.env.version
                for outcome in self.outcomes
                if outcome.env.library == library
            ),
            key=lambda s: tuple(map(int, s.split('.')))
            ))
            header = ['Case'] + versions
            list_of_rows = [header]
            for case in cases:
                version_map = {outcome.env.version: outcome.is_success
                    for outcome in self.outcomes
                    if outcome.env.library == library and outcome.case == case
                }
                row = ['-'] * len(versions)
                for i, version in enumerate(versions):
                    is_success = version_map.get(version)
                    if is_success is not None:
                        row[i] = 'V' if is_success else 'X'
                list_of_rows.append([case] + row)
            ret += self._markdown_table(list_of_rows)

        return ret + self.tail_text()


class Case(abc.ABC):
    def __init__(self, venv: VEnv, logger, dirname=None, leaves_path=None):
        self.venv = venv
        self.dirname = dirname
        self.leaves_path = leaves_path
        self.logger = logger

    def prepare_dir(self):
        if not self.dirname:
            self.delete_dir = True
            self.dirname = tempfile.mkdtemp(prefix='matrixtest')
        else:
            self.delete_dir = False
            self.dirname = os.path.abspath(self.dirname)
            os.makedirs(self.dirname, exist_ok=True)
        self.logger.info(f'Dir: {self.dirname} (delete: {self.delete_dir})')

    def run_python(self):
        with dir_changer(self.dirname, delete_dir=False):
            script_filename = f'{self.__class__.__name__.lower()}.py'
            with open(script_filename, 'w', encoding='utf-8') as fout:
                fout.write(self.python_code())

            execute_wrapper([self.venv.python_path, script_filename])

    def run_go(self):
        with dir_changer(self.dirname, delete_dir=False):
            script_filename = f'{self.__class__.__name__.lower()}.go'
            with open(script_filename, 'w', encoding='utf-8') as fout:
                fout.write(self.go_code())

            if self.leaves_path:
                with open('go.mod', 'w', encoding='utf-8') as fout:
                    fout.write(f"""
module main

require "github.com/zhongdai/leaves" v0.0.0
replace "github.com/zhongdai/leaves" v0.0.0 => "{self.leaves_path}"
""")

            self.logger.info(f'Build {script_filename}')
            execute_wrapper(['go', 'build', script_filename])

            executable_filename = script_filename[:-3]
            if not os.path.isfile(executable_filename):
                raise RuntimeError(f'no executable found: {executable_filename}')

            self.logger.info(f'Run {executable_filename}')
            execute_wrapper([f'./{executable_filename}'])

    def compare_matrices(
        self,
        matrix1_filename,
        matrix2_filename,
        tolerance=1e-9,
        max_number_of_mismatches_ratio=0.0
    ):
        self.logger.info(f"Compare matrices from files: '{matrix1_filename}' and '{matrix2_filename}'")
        matrix1_filename_full = os.path.join(self.dirname, matrix1_filename)
        matrix2_filename_full = os.path.join(self.dirname, matrix2_filename)
        m1 = np.genfromtxt(matrix1_filename_full, delimiter='\t')
        m2 = np.genfromtxt(matrix2_filename_full, delimiter='\t')
        if m1.shape != m2.shape:
            raise CompareError(f'm1.shape != m2.shape ({m1.shape} != {m2.shape})')
        number_of_mismatches = np.sum(np.abs(m1 - m2) > tolerance)
        if number_of_mismatches > max_number_of_mismatches_ratio * m1.size:
            raise CompareError(f'number of mismatches = {number_of_mismatches} (maximum allowed {max_number_of_mismatches_ratio * m1.size}')

    def run(self, venv: VEnv):
        self.logger.info(f'Run case: {self.__class__.__name__} on {venv.env_name}')
        try:
            self.prepare_dir()
            self.run_python()
            self.run_go()
            self.compare()
        except Exception as e:
            self.logger.exception('')
            return False, str(e)
        finally:
            if self.delete_dir:
                shutil.rmtree(self.dirname)
        return True, ''

    @abc.abstractmethod
    def compare(self):
        """
        `compare` method is dedicated to compare output from python lib and
        `leaves` code. `CompareError` will be raised if comparison failed
        """

    @abc.abstractmethod
    def python_code(self) -> str:
        """return python code to run"""

    @abc.abstractmethod
    def go_code(self) -> str:
        """return go code to run"""
