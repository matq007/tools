#!/usr/bin/env python
""" Linting code for the nf-core python package.

Tests Nextflow pipelines to check that they adhere to
the nf-core community guidelines.
"""

import logging
import io
import os
import re
import shlex

import click
import requests
import yaml

import nf_core.utils

# Set up local caching for requests to speed up remote queries
nf_core.utils.setup_requests_cachedir()

# Don't pick up debug logs from the requests package
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

def run_linting(pipeline_dir, release):
    """ Run all linting tests. Called by main script. """

    # Create the lint object
    lint_obj = PipelineLint(pipeline_dir)

    # Run the linting tests
    try:
        lint_obj.lint_pipeline(release)
    except AssertionError as e:
        logging.critical("Critical error: {}".format(e))
        logging.info("Stopping tests...")
        lint_obj.print_results()
        return lint_obj

    # Print the results
    lint_obj.print_results()

    # Exit code
    if len(lint_obj.failed) > 0:
        logging.error(
            "Sorry, some tests failed - exiting with a non-zero error code...{}\n\n"
            .format("\n       Reminder: Lint tests were run in --release mode." if release else '')
        )

    return lint_obj


class PipelineLint(object):
    """ Object to hold linting info and results """

    def __init__(self, pipeline_dir):
        """ Initialise linting object """
        self.releaseMode = False
        self.path = pipeline_dir
        self.files = []
        self.config = {}
        self.pipeline_name = None
        self.minNextflowVersion = None
        self.dockerfile = []
        self.singularityfile = []
        self.conda_config = {}
        self.conda_package_info = {}
        self.passed = []
        self.warned = []
        self.failed = []

    def lint_pipeline(self, release=False):
        """ Main linting function.

        Takes the pipeline directory as the primary input and iterates through
        the different linting checks in order. Collects any warnings or errors
        and returns summary at completion. Raises an exception if there is a
        critical error that makes the rest of the tests pointless (eg. no
        pipeline script). Results from this function are printed by the main script.

        Args:
            pipeline_dir (str): The path to the pipeline directory

        Returns:
            dict: Summary of test result messages structured as follows:
            {
                'pass': [
                    ( test-id (int), message (string) ),
                    ( test-id (int), message (string) )
                ],
                'warn': [(id, msg)],
                'fail': [(id, msg)],
            }

        Raises:
            If a critical problem is found, an AssertionError is raised.
        """
        check_functions = [
            'check_files_exist',
            'check_licence',
            'check_docker',
            'check_singularity',
            'check_nextflow_config',
            'check_ci_config',
            'check_readme',
            'check_conda_env_yaml',
            'check_conda_dockerfile',
            'check_conda_singularityfile',
            'check_pipeline_todos'
        ]
        if release:
            self.releaseMode = True
            check_functions.extend([
                'check_version_consistency'
            ])
        with click.progressbar(check_functions, label='Running pipeline tests', item_show_func=repr) as fnames:
            for fname in fnames:
                getattr(self, fname)()
                if len(self.failed) > 0:
                    logging.error("Found test failures in '{}', halting lint run.".format(fname))
                    break

    def check_files_exist(self):
        """ Check a given pipeline directory for required files.

        Throws an AssertionError if neither nextflow.config or main.nf found
        Gives either test failures or warnings for set of other filenames
        """

        # NB: Should all be files, not directories
        # Supplying a list means if any are present it's a pass
        files_fail = [
            'nextflow.config',
            'Dockerfile',
            'Singularity',
            ['.travis.yml', '.circle.yml'],
            ['LICENSE', 'LICENSE.md', 'LICENCE', 'LICENCE.md'], # NB: British / American spelling
            'README.md',
            'CHANGELOG.md',
            'docs/README.md',
            'docs/output.md',
            'docs/usage.md',
        ]
        files_warn = [
            'main.nf',
            'environment.yml',
            'conf/base.config'
        ]

        def pf(file_path):
            return os.path.join(self.path, file_path)

        # First - critical files. Check that this is actually a Nextflow pipeline
        if not os.path.isfile(pf('nextflow.config')) and not os.path.isfile(pf('main.nf')):
            raise AssertionError('Neither nextflow.config or main.nf found! Is this a Nextflow pipeline?')

        # Files that cause an error
        for files in files_fail:
            if not isinstance(files, list):
                files = [files]
            if any([os.path.isfile(pf(f)) for f in files]):
                self.passed.append((1, "File found: {}".format(files)))
                self.files.extend(files)
            else:
                self.failed.append((1, "File not found: {}".format(files)))

        # Files that cause a warning
        for files in files_warn:
            if not isinstance(files, list):
                files = [files]
            if any([os.path.isfile(pf(f)) for f in files]):
                self.passed.append((1, "File found: {}".format(files)))
                self.files.extend(files)
            else:
                self.warned.append((1, "File not found: {}".format(files)))

        # Load and parse files for later
        if 'environment.yml' in self.files:
            with open(os.path.join(self.path, 'environment.yml'), 'r') as fh:
                self.conda_config = yaml.load(fh)


    def check_docker(self):
        """ Check that Dockerfile contains the string 'FROM ' """
        fn = os.path.join(self.path, "Dockerfile")
        content = ""
        with open(fn, 'r') as fh: content = fh.read()

        # Implicitely also checks if empty.
        if 'FROM ' in content:
            self.passed.append((2, "Dockerfile check passed"))
            self.dockerfile = [line.strip() for line in content.splitlines()]
            return

        self.failed.append((2, "Dockerfile check failed"))

    def check_singularity(self):
        """ Check that Singularity file contains the string 'FROM ' """
        fn = os.path.join(self.path, "Singularity")
        content = ""
        with open(fn, 'r') as fh: content = fh.read()

        # Implicitely also checks if empty.
        if 'From:' in content:
            self.passed.append((2, "Singularity file check passed"))
            self.singularityfile = [line.strip() for line in content.splitlines()]
            return

        self.failed.append((2, "Singularity file check failed"))


    def check_licence(self):
        """ Check licence file is MIT

        Ensures that Licence file is long enough (4 or more lines)
        Checks that licence contains the string 'without restriction'
        Checks that licence doesn't have any placeholder variables
        """
        for l in ['LICENSE', 'LICENSE.md', 'LICENCE', 'LICENCE.md']:
            fn = os.path.join(self.path, l)
            if os.path.isfile(fn):
                content = ""
                with open(fn, 'r') as fh: content = fh.read()

                # needs at least copyright, permission, notice and "as-is" lines
                nl = content.count("\n")
                if nl < 4:
                    self.failed.append((3, "Number of lines too small for a valid MIT license file: {}".format(fn)))
                    return

                # determine whether this is indeed an MIT
                # license. Most variations actually don't contain the
                # string MIT Searching for 'without restriction'
                # instead (a crutch).
                if not 'without restriction' in content:
                    self.failed.append((3, "Licence file did not look like MIT: {}".format(fn)))
                    return

                # check for placeholders present in
                # - https://choosealicense.com/licenses/mit/
                # - https://opensource.org/licenses/MIT
                # - https://en.wikipedia.org/wiki/MIT_License
                placeholders = set(['[year]', '[fullname]',
                                    '<YEAR>', '<COPYRIGHT HOLDER>',
                                    '<year>', '<copyright holders>'])
                if any([ph in content for ph in placeholders]):
                    self.failed.append((3, "Licence file contains placeholders: {}".format(fn)))
                    return

                self.passed.append((3, "Licence check passed"))
                return

        self.failed.append((3, "Couldn't find MIT licence file"))


    def check_nextflow_config(self):
        """ Check a given pipeline for required config variables.

        Uses `nextflow config -flat` to parse pipeline nextflow.config
        and print all config variables.
        NB: Does NOT parse contents of main.nf / nextflow script
        """
        # Fail tests if these are missing
        config_fail = [
            'manifest.name',
            'manifest.nextflowVersion',
            'manifest.description',
            'manifest.version',
            'manifest.homePage',
            'timeline.enabled',
            'trace.enabled',
            'report.enabled',
            'dag.enabled',
            'process.cpus',
            'process.memory',
            'process.time',
            'params.outdir'
        ]
        # Throw a warning if these are missing
        config_warn = [
            'manifest.mainScript',
            'timeline.file',
            'trace.file',
            'report.file',
            'dag.file',
            'params.reads',
            'process.container',
            'params.container',
            'params.singleEnd'
        ]
        # Old depreciated vars - fail if present
        config_fail_ifdefined = [
            'params.version',
            'params.nf_required_version'
        ]

        # Get the nextflow config for this pipeline
        self.config = nf_core.utils.fetch_wf_config(self.path)
        for cf in config_fail:
            if cf in self.config.keys():
                self.passed.append((4, "Config variable found: {}".format(cf)))
            else:
                self.failed.append((4, "Config variable not found: {}".format(cf)))
        for cf in config_warn:
            if cf in self.config.keys():
                self.passed.append((4, "Config variable found: {}".format(cf)))
            else:
                self.warned.append((4, "Config variable not found: {}".format(cf)))
        for cf in config_fail_ifdefined:
            if cf not in self.config.keys():
                self.passed.append((4, "Config variable (correctly) not found: {}".format(cf)))
            else:
                self.failed.append((4, "Config variable (incorrectly) found: {}".format(cf)))

        # Check and warn if the process configuration is done with deprecated syntax
        process_with_deprecated_syntax = list(set([re.search('^(process\.\$.*?)\.+.*$', ck).group(1) for ck in self.config.keys() if re.match(r'^(process\.\$.*?)\.+.*$', ck)]))
        for pd in process_with_deprecated_syntax:
            self.warned.append((4, "Process configuration is done with deprecated_syntax: {}".format(pd)))

        # Check the variables that should be set to 'true'
        for k in ['timeline.enabled', 'report.enabled', 'trace.enabled', 'dag.enabled']:
            if self.config.get(k) == 'true':
                self.passed.append((4, "Config variable '{}' had correct value: {}".format(k, self.config.get(k))))
            else:
                self.failed.append((4, "Config variable '{}' did not have correct value: {}".format(k, self.config.get(k))))

        # Check that the pipeline name starts with nf-core
        try:
            assert self.config.get('manifest.name', '').strip('\'"').startswith('nf-core/')
        except (AssertionError, IndexError):
            self.failed.append((4, "Config variable 'manifest.name' did not begin with nf-core/:\n    {}".format(self.config.get('manifest.name', '').strip('\'"'))))
        else:
            self.passed.append((4, "Config variable 'manifest.name' began with 'nf-core/'"))
            self.pipeline_name = self.config.get('manifest.name', '').strip("'").replace('nf-core/', '')

        # Check that the homePage is set to the GitHub URL
        try:
            assert self.config.get('manifest.homePage', '').strip('\'"').startswith('https://github.com/nf-core/')
        except (AssertionError, IndexError):
            self.failed.append((4, "Config variable 'manifest.homePage' did not begin with https://github.com/nf-core/:\n    {}".format(self.config.get('manifest.homePage', '').strip('\'"'))))
        else:
            self.passed.append((4, "Config variable 'manifest.homePage' began with 'https://github.com/nf-core/'"))

        # Check that the DAG filename ends in `.svg`
        if 'dag.file' in self.config:
            if self.config['dag.file'].strip('\'"').endswith('.svg'):
                self.passed.append((4, "Config variable 'dag.file' ended with .svg"))
            else:
                self.failed.append((4, "Config variable 'dag.file' did not end with .svg"))

        # Check that the minimum nextflowVersion is set properly
        if 'manifest.nextflowVersion' in self.config:
            if self.config.get('manifest.nextflowVersion', '').strip('"\'').startswith('>='):
                self.passed.append((4, "Config variable 'manifest.nextflowVersion' started with >="))
                # Save self.minNextflowVersion for convenience
                self.minNextflowVersion = re.sub(r'[^0-9\.]', '', self.config.get('manifest.nextflowVersion', ''))
            else:
                self.failed.append((4, "Config variable 'manifest.nextflowVersion' did not start with '>=' : '{}'".format(self.config.get('manifest.nextflowVersion', '')).strip('"\'')))

    def check_ci_config(self):
        """ Check that the Travis or Circle CI YAML config is valid

        Makes sure that `nf-core lint` runs in travis tests
        Checks that tests run with the required nextflow version
        """

        for cf in ['.travis.yml', 'circle.yml']:
            fn = os.path.join(self.path, cf)
            if os.path.isfile(fn):
                with open(fn, 'r') as fh:
                    ciconf = yaml.load(fh)
                # Check that we have the master branch protection
                travisMasterCheck = '[ $TRAVIS_PULL_REQUEST = "false" ] || [ $TRAVIS_BRANCH != "master" ] || ([ $TRAVIS_PULL_REQUEST_SLUG = $TRAVIS_REPO_SLUG ] && [ $TRAVIS_PULL_REQUEST_BRANCH = "dev" ])'
                try:
                    assert(travisMasterCheck in ciconf.get('before_install', {}))
                except AssertionError:
                    self.failed.append((5, "Continuous integration must check for master branch PRs: '{}'".format(fn)))
                else:
                    self.passed.append((5, "Continuous integration checks for master branch PRs: '{}'".format(fn)))
                # Check that the nf-core linting runs
                try:
                    assert('nf-core lint ${TRAVIS_BUILD_DIR}' in ciconf['script'])
                except AssertionError:
                    self.failed.append((5, "Continuous integration must run nf-core lint Tests: '{}'".format(fn)))
                else:
                    self.passed.append((5, "Continuous integration runs nf-core lint Tests: '{}'".format(fn)))
                # Check that we're pulling the right docker image
                if self.config.get('params.container', ''):
                    docker_notag = re.sub(r':(?:[\.\d]+|latest)$', '', self.config.get('params.container', '').strip('"\''))
                    docker_pull_cmd = 'docker pull {}:dev'.format(docker_notag)
                    try:
                        assert(docker_pull_cmd in ciconf.get('before_install', []))
                    except AssertionError:
                        self.failed.append((5, "CI is not pulling the correct docker image. Should be:\n    '{}'".format(docker_pull_cmd)))
                    else:
                        self.passed.append((5, "CI is pulling the correct docker image: {}".format(docker_pull_cmd)))

                    # Check that we tag the docker image properly
                    docker_tag_cmd = 'docker tag {}:dev {}'.format(docker_notag, self.config.get('params.container', '').strip('"\''))
                    try:
                        assert(docker_tag_cmd in ciconf.get('before_install'))
                    except AssertionError:
                        self.failed.append((5, "CI is not tagging docker image correctly. Should be:\n    '{}'".format(docker_tag_cmd)))
                    else:
                        self.passed.append((5, "CI is tagging docker image correctly: {}".format(docker_tag_cmd)))

                # Check that we're testing the minimum nextflow version
                minNextflowVersion = ""
                env = ciconf.get('env', [])
                if type(env) is dict:
                    env = env.get('matrix', [])
                for e in env:
                    # Split using shlex so that we don't split "quoted whitespace"
                    for s in shlex.split(e):
                        k,v = s.split('=')
                        if k == 'NXF_VER':
                            ci_ver = v.strip('\'"')
                            minNextflowVersion = ci_ver if v else minNextflowVersion
                            if ci_ver == self.minNextflowVersion:
                                self.passed.append((5, "Continuous integration checks minimum NF version: '{}'".format(fn)))
                if not minNextflowVersion:
                    self.failed.append((5, "Continuous integration does not check minimum NF version: '{}'".format(fn)))
                elif minNextflowVersion != self.minNextflowVersion:
                    self.failed.append((5, "Minimum NF version differed from CI and what was set in the pipelines manifest: {}".format(fn)))

    def check_readme(self):
        """ Check the repository README file for errors

        Currently just checks the badges at the top of the README
        """
        with open(os.path.join(self.path, 'README.md'), 'r') as fh:
            content = fh.read()

        # Check that there is a readme badge showing the minimum required version of Nextflow
        # and that it has the correct version
        nf_badge_re = r"\[!\[Nextflow\]\(https://img\.shields\.io/badge/nextflow-%E2%89%A5([\d\.]+)-brightgreen\.svg\)\]\(https://www\.nextflow\.io/\)"
        match = re.search(nf_badge_re, content)
        if match:
            nf_badge_version = match.group(1).strip('\'"')
            try:
                assert nf_badge_version == self.minNextflowVersion
            except (AssertionError, KeyError):
                self.failed.append((6, "README Nextflow minimum version badge does not match config. Badge: '{}', Config: '{}'".format(nf_badge_version, self.minNextflowVersion)))
            else:
                self.passed.append((6, "README Nextflow minimum version badge matched config. Badge: '{}', Config: '{}'".format(nf_badge_version, self.minNextflowVersion)))
        else:
            self.warned.append((6, "README did not have a Nextflow minimum version badge."))

        # Check that we have a bioconda badge if we have a bioconda environment file
        if 'environment.yml' in self.files:
            bioconda_badge = '[![install with bioconda](https://img.shields.io/badge/install%20with-bioconda-brightgreen.svg)](http://bioconda.github.io/)'
            if bioconda_badge in content:
                self.passed.append((6, "README had a bioconda badge"))
            else:
                self.failed.append((6, "Found a bioconda environment.yml file but no badge in the README"))


    def check_version_consistency(self):
        """ Check container tags versions

        Runs on process.container, params.container and $TRAVIS_TAG (each only if set)
        Check that the container has a tag
        Check that the version numbers are numeric
        Check that the version numbers are the same as one-another """

        versions = {}
        # Get the version definitions
        # Get version from nextflow.config
        versions['manifest.version'] = self.config.get('manifest.version', '').strip(' \'"')

        # Get version from the docker slug
        if self.config.get('params.container', '') and \
                not ':' in self.config.get('params.container', ''):
            self.failed.append((7, "Docker slug seems not to have "
                "a version tag: {}".format(self.config.get('params.container', ''))))
            return

        # Get config container slugs, (if set; one container per workflow)
        if self.config.get('params.container', ''):
            versions['params.container'] = self.config.get('params.container', '').strip(' \'"').split(':')[-1]
        if self.config.get('process.container', ''):
            versions['process.container'] = self.config.get('process.container', '').strip(' \'"').split(':')[-1]

        # Get version from the TRAVIS_TAG env var
        if os.environ.get('TRAVIS_TAG') and os.environ.get('TRAVIS_REPO_SLUG', '') != 'nf-core/tools':
            versions['TRAVIS_TAG'] = os.environ.get('TRAVIS_TAG').strip(' \'"')

        # Check if they are all numeric
        for v_type, version in versions.items():
            if not version.replace('.', '').isdigit():
                self.failed.append((7, "{} was not numeric: {}!".format(v_type, version)))
                return

        # Check if they are consistent
        if len(set(versions.values())) != 1:
            self.failed.append((7, "The versioning is not consistent between container, release tag "
                "and config. Found {}".format(
                    ", ".join(["{} = {}".format(k, v) for k,v in versions.items()])
                )))
            return

        self.passed.append((7, "Version tags are numeric and consistent between container, release tag and config."))


    def check_conda_env_yaml(self):
        """ Check that the conda environment file is valid

        Make sure that a name is given and is consistent with the pipeline name
        Check that depedency versions are pinned
        Warn if dependency versions are not the latest available """

        if 'environment.yml' not in self.files:
            return

        # Check that the environment name matches the pipeline name
        pipeline_version = self.config.get('manifest.version', '').strip(' \'"')
        expected_env_name = 'nf-core-{}-{}'.format(self.pipeline_name.lower(), pipeline_version)
        if self.conda_config['name'] != expected_env_name:
            self.failed.append((8, "Conda environment name is incorrect ({}, should be {})".format(self.conda_config['name'], expected_env_name)))
        else:
            self.passed.append((8, "Conda environment name was correct ({})".format(expected_env_name)))

        # Check conda dependency list
        for dep in self.conda_config.get('dependencies', []):
            if isinstance(dep, str):
                # Check that each dependency has a version number
                try:
                    assert dep.count('=') == 1
                except AssertionError:
                    self.failed.append((8, "Conda dependency did not have pinned version number: {}".format(dep)))
                else:
                    self.passed.append((8, "Conda dependency had pinned version number: {}".format(dep)))

                    try:
                        depname, depver = dep.split('=', 1)
                        self.check_anaconda_package(dep)
                    except ValueError:
                        pass
                    else:
                        # Check that required version is available at all
                        if depver not in self.conda_package_info[dep].get('versions'):
                            self.failed.append((8, "Conda dependency had an unknown version: {}".format(dep)))
                            continue  # No need to test for latest version, continue linting
                        # Check version is latest available
                        last_ver = self.conda_package_info[dep].get('latest_version')
                        if last_ver is not None and last_ver != depver:
                            self.warned.append((8, "Conda package is not latest available: {}, {} available".format(dep, last_ver)))
                        else:
                            self.passed.append((8, "Conda package is latest available: {}".format(dep)))

            elif isinstance(dep, dict):
                for pip_dep in dep.get('pip', []):
                    # Check that each pip dependency has a version number
                    try:
                        assert pip_dep.count('=') == 1
                    except AssertionError:
                        self.failed.append((8, "Pip dependency did not have pinned version number: {}".format(pip_dep)))
                    else:
                        self.passed.append((8, "Pip dependency had pinned version number: {}".format(pip_dep)))

                        try:
                            pip_depname, pip_depver = pip_dep.split('=', 1)
                            self.check_pip_package(pip_dep)
                        except ValueError:
                            pass
                        else:
                            # Check, if PyPi package version is available at all
                            if pip_depver not in self.conda_package_info[pip_dep].get('releases').keys():
                                self.failed.append((8, "PyPi package had an unknown version: {}".format(pip_depver)))
                                continue  # No need to test latest version, if not available
                            last_ver = self.conda_package_info[pip_dep].get('info').get('version')
                            if last_ver is not None and last_ver != pip_depver:
                                self.warned.append((8, "PyPi package is not latest available: {}, {} available".format(pip_depver, last_ver)))
                            else:
                                self.passed.append((8, "PyPi package is latest available: {}".format(pip_depver)))

    def check_anaconda_package(self, dep):
        """ Call the anaconda API to find details about package """
        # Check if each dependency is the latest available version
        depname, depver = dep.split('=', 1)
        dep_channels = self.conda_config.get('channels', [])
        if '::' in depname:
            dep_channels = [depname.split('::')[0]]
            depname = depname.split('::')[1]
        for ch in reversed(dep_channels):
            anaconda_api_url = 'https://api.anaconda.org/package/{}/{}'.format(ch, depname)
            try:
                response = requests.get(anaconda_api_url, timeout=10)
            except (requests.exceptions.Timeout):
                self.warned.append((8, "Anaconda API timed out: {}".format(anaconda_api_url)))
                raise ValueError
            except (requests.exceptions.ConnectionError):
                self.warned.append((8, "Could not connect to Anaconda API"))
                raise ValueError
            else:
                if response.status_code == 200:
                    dep_json = response.json()
                    self.conda_package_info[dep] = dep_json
                    return
        else:
            self.failed.append((8, "Could not find Conda dependency using the Anaconda API: {}".format(dep)))
            raise ValueError

    def check_pip_package(self, dep):
        """ Call the PyPI API to find details about package """
        pip_depname, pip_depver = dep.split('=', 1)
        pip_api_url = 'https://pypi.python.org/pypi/{}/json'.format(pip_depname)
        try:
            response = requests.get(pip_api_url, timeout=10)
        except (requests.exceptions.Timeout):
            self.warned.append((8, "PyPi API timed out: {}".format(pip_api_url)))
            raise ValueError
        except (requests.exceptions.ConnectionError):
            self.warned.append((8, "PyPi API Connection error: {}".format(pip_api_url)))
            raise ValueError
        else:
            if response.status_code == 200:
                pip_dep_json = response.json()
                self.conda_package_info[dep] = pip_dep_json
            else:
                self.failed.append((8, "Could not find pip dependency using the PyPi API: {}".format(dep)))
                raise ValueError

    def check_conda_dockerfile(self):
        """ Check that the Docker build file looks right, if working with conda

        Make sure that a name is given and is consistent with the pipeline name
        Check that depedency versions are pinned
        Warn if dependency versions are not the latest available """

        if 'environment.yml' not in self.files or len(self.dockerfile) == 0:
            return

        expected_strings = [
            'FROM nfcore/base',
            'COPY environment.yml /',
            'RUN conda env create -f /environment.yml && conda clean -a',
            'ENV PATH /opt/conda/envs/{}/bin:$PATH'.format(self.conda_config['name'])
        ]

        difference = set(expected_strings) - set(self.dockerfile)
        if not difference:
            self.passed.append((10, "Found all expected strings in Dockerfile file"))
        else:
            for missing in difference:
                self.failed.append((10, "Could not find Dockerfile file string: {}".format(missing)))

    def check_conda_singularityfile(self):
        """ Check that the Singularity build file looks right, if working with conda

        Make sure that a name is given and is consistent with the pipeline name
        Check that depedency versions are pinned
        Warn if dependency versions are not the latest available """

        if 'environment.yml' not in self.files or len(self.singularityfile) == 0:
            return

        expected_strings = [
            'From:nfcore/base',
            'Bootstrap:docker',
            'VERSION {}'.format(self.config.get('manifest.version', '').strip(' \'"')),
            'PATH=/opt/conda/envs/{}/bin:$PATH'.format(self.conda_config['name']),
            'export PATH',
            'environment.yml /',
            '/opt/conda/bin/conda env create -f /environment.yml',
            '/opt/conda/bin/conda clean -a',
        ]

        difference = set(expected_strings) - set(self.singularityfile)
        if not difference:
            self.passed.append((10, "Found all expected strings in Singularity file"))
        else:
            for missing in difference:
                self.failed.append((10, "Could not find Singularity file string: {}".format(missing)))

    def check_pipeline_todos(self):
        """ Go through all template files looking for the string 'TODO nf-core:' """
        ignore = ['.git']
        if os.path.isfile(os.path.join(self.path, '.gitignore')):
            with io.open(os.path.join(self.path, '.gitignore'), 'rt', encoding='latin1') as fh:
                for l in fh:
                    ignore.append(os.path.basename(l.strip().rstrip('/')))
        for root, dirs, files in os.walk(self.path):
            # Ignore files
            for i in ignore:
                if i in dirs:
                    dirs.remove(i)
                if i in files:
                    files.remove(i)
            for fname in files:
                with io.open(os.path.join(root, fname), 'rt', encoding='latin1') as fh:
                    for l in fh:
                        if 'TODO nf-core' in l:
                            l = l.replace('<!--', '').replace('-->', '').replace('# TODO nf-core: ', '').replace('// TODO nf-core: ', '').replace('TODO nf-core: ', '').strip()
                            if len(fname) + len(l) > 50:
                                l = '{}..'.format(l[:50-len(fname)])
                            self.warned.append((11, "TODO string found in '{}': {}".format(fname,l)))

    def print_results(self):
        # Print results
        rl = "\n  Using --release mode linting tests" if self.releaseMode else ''
        logging.info("===========\n LINTING RESULTS\n=================\n" +
            "{0:>4} tests passed".format(len(self.passed)) +
            "{0:>4} tests had warnings".format(len(self.warned)) +
            "{0:>4} tests failed".format(len(self.failed)) + rl
        )
        if len(self.passed) > 0:
            logging.debug("Test Passed:\n  {}".format("\n  ".join(["http://nf-co.re/errors#{}: {}".format(eid, msg) for eid, msg in self.passed])))
        if len(self.warned) > 0:
            logging.warn("Test Warnings:\n  {}".format("\n  ".join(["http://nf-co.re/errors#{}: {}".format(eid, msg) for eid, msg in self.warned])))
        if len(self.failed) > 0:
            logging.error("Test Failures:\n  {}".format("\n  ".join(["http://nf-co.re/errors#{}: {}".format(eid, msg) for eid, msg in self.failed])))
