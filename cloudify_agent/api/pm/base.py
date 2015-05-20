#########
# Copyright (c) 2013 GigaSpaces Technologies Ltd. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
#  * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  * See the License for the specific language governing permissions and
#  * limitations under the License.

import getpass
import uuid
import os
import time
import json
import logging

from celery import Celery

from cloudify.utils import LocalCommandRunner
from cloudify.utils import setup_logger
from cloudify import amqp_client

from cloudify_agent import VIRTUALENV
from cloudify_agent.api import utils
from cloudify_agent.api import errors
from cloudify_agent.api import exceptions
from cloudify_agent.api import defaults
from cloudify_agent.api.utils import get_storage_directory
from cloudify_agent.api.factory import DaemonFactory
from cloudify_agent.included_plugins import included_plugins


class Daemon(object):

    """
    Base class for daemon implementations.
    Following is all the available common daemon keyword arguments:

    ``manager_ip``:

        the ip address of the manager host. (Required)

    ``user``:

        the user this daemon will run under. default to the current user.

    ``name``:

        the name to give the daemon. This name will be a unique identifier of
        the daemon. meaning you will not be able to create more daemons with
        that name until a delete operation has been performed. defaults to
        a unique name generated by cloudify.

    ``queue``:

        the queue this daemon will listen to. It is possible to create
        different workers with the same queue, however this is discouraged.
        to create more workers that process tasks of a given queue, use the
        'min_workers' and 'max_workers' keys. defaults to <name>-queue.

    ``workdir``:

        working directory for runtime files (pid, log).
        defaults to the current working directory.

    ``broker_ip``:

        the ip address of the broker to connect to.
        defaults to the manager_ip value.

    ``broker_port``

        the connection port of the broker process.
        defaults to 5672.

    ``broker_url``:

        full url to the broker. if this key is specified,
        the broker_ip and broker_port keys are ignored.

        for example:
            amqp://192.168.9.19:6786

        if this is not specified, the broker url will be constructed from the
        broker_ip and broker_port like so:
        'amqp://guest:guest@<broker_ip>:<broker_port>//'

    ``manager_port``:

        the manager REST gateway port to connect to. defaults to 80.

    ``min_workers``:

        the minimum number of worker processes this daemon will manage. all
        workers will listen on the same queue allowing for higher
        concurrency when preforming tasks. defaults to 0.

    ``max_workers``:

        the maximum number of worker processes this daemon will manage.
        as tasks keep coming in, the daemon will expand its worker pool to
        handle more tasks concurrently. However, as the name
        suggests, it will never exceed this number. allowing for the control
        of resource usage. defaults to 5.

    ``extra_env_path``:

        path to a file containing environment variables to be added to the
        daemon environment. the file should be in the format of
        multiple 'export A=B' lines. defaults to None.

    ``plugins``:

        a comma separated list of plugin names to be included in the daemon.

    """

    # override this when adding implementations.
    PROCESS_MANAGEMENT = None

    # add specific mandatory parameters for different implementations
    # they will be validated upon daemon creation
    MANDATORY_PARAMS = [
        'manager_ip'
    ]

    def __init__(self,
                 logger_level=logging.INFO,
                 logger_format=None,
                 **params):

        """

        ####################################################################
        # When subclassing this, do not implement any logic inside the
        # constructor expect for in-memory calculations and settings, as the
        # agent may be instantiated many times for an existing agent.
        ####################################################################

        :param logger_level: logging level for the daemon operations.
        :type logger_level: int

        :param params: key-value pairs as stated above.
        :type params dict

        :return: an instance of a daemon.
        :rtype `cloudify_agent.api.pm.base.Daemon`
        """

        # Mandatory parameters
        self.validate_mandatory(params)

        self.manager_ip = params['manager_ip']

        # Optional parameters
        self.validate_optional(params)

        self.user = params.get('user') or getpass.getuser()
        self.broker_ip = params.get(
            'broker_ip') or self.manager_ip
        self.broker_port = params.get(
            'broker_port') or defaults.BROKER_PORT
        self.name = params.get(
            'name') or utils.generate_agent_name()
        self.queue = params.get(
            'queue') or '{0}-queue'.format(self.name)
        self.broker_url = params.get(
            'broker_url') or defaults.BROKER_URL.format(
            self.broker_ip,
            self.broker_port)
        self.manager_port = params.get(
            'manager_port') or defaults.MANAGER_PORT
        self.min_workers = params.get(
            'min_workers') or defaults.MIN_WORKERS
        self.max_workers = params.get(
            'max_workers') or defaults.MAX_WORKERS
        self.workdir = params.get(
            'workdir') or os.getcwd()

        # accept a comma separated list as the plugins to include
        # with the agent.
        plugins = params.get('plugins')
        if plugins:
            if isinstance(plugins, str):
                self.plugins = plugins.split(',')
            elif isinstance(plugins, list):
                self.plugins = plugins
            else:
                raise ValueError("Unexpected type of attribute 'plugins'. "
                                 "Expected either 'str' or 'list', but got: "
                                 "{0}".format(type(plugins)))
        else:
            self.plugins = []

        # add included plugins
        for included_plugin in included_plugins:
            if included_plugin not in self.plugins:
                self.plugins.append(included_plugin)

        # create working directory if its missing
        if not os.path.exists(self.workdir):
            os.makedirs(self.workdir)

        self.extra_env_path = params.get('extra_env_path')

        # save as a property so that it will be persisted in the json files
        self.process_management = self.PROCESS_MANAGEMENT

        # save as a property so that it will be persisted in the json files
        self.virtualenv = VIRTUALENV

        # save as a property so that it will be persisted in the json files
        self.logger_level = logger_level

        # save as a property so that it will be persisted in the json files
        self.logger_format = logger_format

        # configure logger
        self.logger = setup_logger(
            logger_name='cloudify_agent.api.pm.{0}'
                        .format(self.PROCESS_MANAGEMENT),
            logger_level=logger_level,
            logger_format=logger_format)

        # configure command runner
        self.runner = LocalCommandRunner(logger=self.logger)

        # initialize an internal celery client
        self.celery = Celery(broker=self.broker_url,
                             backend=self.broker_url)

    @classmethod
    def validate_mandatory(cls, params):

        """
        Validates that all mandatory parameters are given.

        :param params: parameters of the daemon.
        :type params: dict

        :raise DaemonMissingMandatoryPropertyError:
        in case one of the mandatory parameters is missing.
        """

        for param in cls.MANDATORY_PARAMS:
            if param not in params:
                raise errors.DaemonMissingMandatoryPropertyError(param)

    @staticmethod
    def validate_optional(params):

        """
        Validates any optional parameters given to the daemon.

        :param params: parameters of the daemon.
        :type params: dict

        :raise DaemonPropertiesError:
        in case one of the parameters is faulty.
        """

        min_workers = params.get('min_workers')
        max_workers = params.get('max_workers')

        if min_workers:
            if not str(min_workers).isdigit():
                raise errors.DaemonPropertiesError(
                    'min_workers is supposed to be a number '
                    'but is: {0}'
                    .format(min_workers)
                )
            min_workers = int(min_workers)

        if max_workers:
            if not str(max_workers).isdigit():
                raise errors.DaemonPropertiesError(
                    'max_workers is supposed to be a number '
                    'but is: {0}'
                    .format(max_workers)
                )
            max_workers = int(max_workers)

        if min_workers and max_workers:
            if min_workers > max_workers:
                raise errors.DaemonPropertiesError(
                    'min_workers cannot be greater than max_workers '
                    '[min_workers={0}, max_workers={1}]'
                    .format(min_workers, max_workers))

    ########################################################################
    # the following methods must be implemented by the sub-classes as they
    # may exhibit custom logic
    ########################################################################

    def configure(self):

        """
        Creates any necessary resources for the daemon. This method MUST be
        This method must create all necessary configuration of the daemon.

        :return: The daemon name.
        :rtype: str
        """
        raise NotImplementedError('Must be implemented by a subclass')

    def delete(self, force=defaults.DAEMON_FORCE_DELETE):

        """
        Delete any resources created by the daemon.

        :param force: if the daemon is still running, stop it before
        deleting it.
        :type force: bool
        """
        raise NotImplementedError('Must be implemented by a subclass')

    def update_includes(self, tasks):

        """
        Update the includes list of the agent. This method must modify the
        includes configuration used when starting the agent.
        """
        raise NotImplementedError('Must be implemented by a subclass')

    def start_command(self):

        """
        A command line for starting the daemon.
        (e.g sudo service <name> start)
        """
        raise NotImplementedError('Must be implemented by a subclass')

    def stop_command(self):

        """
        A command line for stopping the daemon.
        (e.g sudo service <name> stop)
        """
        raise NotImplementedError('Must be implemented by a subclass')

    ########################################################################
    # the following methods are the common logic that should apply to any
    # process management implementation.
    ########################################################################

    def register(self, plugin):

        """
        Register an additional plugin. This method will enable the addition
        of operations defined in the plugin.

        :param plugin: The plugin name to register.
        :type plugin: str
        """

        self.logger.debug('Listing modules of plugin: {0}'
                          .format(plugin))
        tasks = utils.list_plugin_files(plugin)
        self.logger.debug('Following modules will be appended to '
                          'includes: {0}'
                          .format(json.dumps(tasks, indent=2)))

        # process management specific implementation
        self.update_includes(tasks)

        # keep track of the plugins regardless of the process management
        # check if the plugin already exists in the state, this can happen
        # because instances can be instantiated with plugins, therefore the
        # registration of such plugins should not add to the state.
        if plugin not in self.plugins:
            self.plugins.append(plugin)

        # save the plugin again because the 'plugins' attribute
        # has changed
        DaemonFactory.save(self)

    def create(self):

        """
        Creates the agent. This method saves the daemon properties in the
        storage folder and register plugins.
        """

        DaemonFactory.save(self)

    def start(self,
              interval=defaults.START_INTERVAL,
              timeout=defaults.START_TIMEOUT,
              delete_amqp_queue=defaults.DELETE_AMQP_QUEUE_BEFORE_START):

        """
        Starts the daemon process.

        :param interval: the interval in seconds to sleep when waiting for
        the daemon to be ready.
        :type interval: int

        :param timeout: the timeout in seconds to wait for the daemon to be
        ready.
        :type timeout: int

        :raise DaemonStartupTimeout: in case the agent failed to start in the
        given amount of time.
        :raise DaemonException: in case an error happened during the agent
        startup.

        """

        if delete_amqp_queue:
            self._delete_amqp_queues()
        self.runner.run(self.start_command())
        end_time = time.time() + timeout
        while time.time() < end_time:
            stats = utils.get_agent_stats(self.name, self.celery)
            if stats:
                return
            time.sleep(interval)
        self._verify_no_celery_error()
        raise exceptions.DaemonStartupTimeout(timeout, self.name)

    def stop(self,
             interval=defaults.STOP_INTERVAL,
             timeout=defaults.STOP_TIMEOUT):

        """
        Stops the daemon process.

        :param interval: the interval in seconds to sleep when waiting for
        the daemon to stop.
        :type interval: int

        :param timeout: the timeout in seconds to wait for the daemon to stop.
        :type timeout: int

        :raise DaemonShutdownTimeout: in case the agent failed to be stopped
        in the given amount of time.
        :raise DaemonException: in case an error happened during the agent
        shutdown.

        """
        self.runner.run(self.stop_command())
        end_time = time.time() + timeout
        while time.time() < end_time:
            stats = utils.get_agent_stats(self.name, self.celery)
            if not stats:
                return
            time.sleep(interval)
        self._verify_no_celery_error()
        raise exceptions.DaemonShutdownTimeout(timeout, self.name)

    def restart(self,
                start_timeout=defaults.START_TIMEOUT,
                start_interval=defaults.START_INTERVAL,
                stop_timeout=defaults.STOP_TIMEOUT,
                stop_interval=defaults.STOP_INTERVAL):

        """
        Restart the daemon process.

        :param start_interval: the interval in seconds to sleep when waiting
        for the daemon to start.
        :type start_interval: int

        :param start_timeout: The timeout in seconds to wait for the daemon
        to start.
        :type start_timeout: int

        :param stop_interval: the interval in seconds to sleep when waiting
        for the daemon to stop.
        :type stop_interval: int

        :param stop_timeout: the timeout in seconds to wait for the daemon
        to stop.
        :type stop_timeout: int

        :raise DaemonStartupTimeout: in case the agent failed to start in the
        given amount of time.
        :raise DaemonShutdownTimeout: in case the agent failed to be stopped
        in the given amount of time.
        :raise DaemonException: in case an error happened during startup or
        shutdown

        """

        self.stop(timeout=stop_timeout,
                  interval=stop_interval)
        self.start(timeout=start_timeout,
                   interval=start_interval)

    def _verify_no_celery_error(self):

        error_dump_path = os.path.join(
            get_storage_directory(),
            '{0}.err'.format(self.name))

        # this means the celery worker had an uncaught
        # exception and it wrote its content
        # to the file above because of our custom exception
        # handler (see app.py)
        if os.path.exists(error_dump_path):
            with open(error_dump_path) as f:
                error = f.read()
            os.remove(error_dump_path)
            raise exceptions.DaemonException(error)

    def _delete_amqp_queues(self):
        client = amqp_client.create_client(self.broker_ip)
        try:
            channel = client.connection.channel()
            channel.queue_delete(self.queue)
            channel.queue_delete('celery@{0}.celery.pidbox'
                                 .format(self.queue))
        finally:
            try:
                client.close()
            except Exception as e:
                self.logger.warning('Failed closing amqp client: {0}'
                                    .format(e))
