# Copyright (C) 2013-2014 SignalFuse, Inc.
#
# Docker container orchestration utility.

import bgtunnel
import datetime
import time
import os

# Import _strptime manually to work around a thread safety issue when using
# strptime() from threads for the first time.
import _strptime # flake8: noqa

import docker
try:
    from docker.errors import APIError
except ImportError:
    # Fall back to <= 0.3.1 location
    from docker.client import APIError

import multiprocessing.pool
import re
import six

# For Python bug workaround
import threading
import weakref

from . import exceptions
from . import lifecycle


# Possible values for the restart policy type.
_VALID_RESTART_POLICIES = ['no', 'always', 'on-failure']


class Entity:
    """Base class for named entities in the orchestrator."""
    def __init__(self, name):
        self._name = name

    @property
    def name(self):
        """Get the name of this entity."""
        return self._name

    def __repr__(self):
        return self._name


class Ship(Entity):
    """A Ship that can host and run Containers.

    Ships are hosts in the infrastructure. A Docker daemon is expected to be
    running on each ship, providing control over the containers that will be
    executed there.
    """

    DEFAULT_DOCKER_PORT = 2375
    DEFAULT_DOCKER_TLS_PORT = 2376
    DEFAULT_DOCKER_VERSION = '1.10'
    DEFAULT_DOCKER_TIMEOUT = 5

    def __init__(self, name, ip, endpoint=None, docker_port=None,
                 socket_path=None, timeout=None, ssh_tunnel=None, tls=None,
                 tls_verify=False, tls_ca_cert=None, tls_cert=None,
                 tls_key=None, ssl_version=None):
        """Instantiate a new ship.

        Args:
            name (string): the name of the ship.
            ip (string): the IP address of resolvable host name of the host.
            docker_port (int): the port the Docker daemon listens on.
            socket_path (string): the path to the unix socket the Docker daemon listens on.
            ssh_tunnel (dict): configuration for SSH tunneling to the remote
                Docker daemon.
        """
        Entity.__init__(self, name)
        self._ip = ip
        self._endpoint = endpoint or ip
        self._docker_port = int(docker_port or
                (self.DEFAULT_DOCKER_TLS_PORT if tls else self.DEFAULT_DOCKER_PORT))
        self._socket_path = os.path.realpath(socket_path) if socket_path else None
        self._tunnel = None

        if ssh_tunnel:
            if 'user' not in ssh_tunnel:
                raise exceptions.EnvironmentConfigurationException(
                    'Missing SSH user for ship {} tunnel configuration'.format(
                        self.name))
            if 'key' not in ssh_tunnel:
                raise exceptions.EnvironmentConfigurationException(
                    'Missing SSH key for ship {} tunnel configuration'.format(
                        self.name))

            self._tunnel = bgtunnel.open(
                ssh_address=self._endpoint,
                ssh_user=ssh_tunnel['user'],
                ssh_port=int(ssh_tunnel.get('port', 22)),
                host_port=self._docker_port,
                silent=True,
                identity_file=ssh_tunnel['key'])

            # Make sure we use https through the tunnel, if tls is enabled
            proto = "https" if (tls or tls_verify) else "http"
            self._backend_url = '{:s}://localhost:{:d}'.format(
                proto, self._tunnel.bind_port)

            # Apparently bgtunnel isn't always ready right away and this
            # drastically cuts down on the timeouts
            time.sleep(1)

        elif self._socket_path is not None:
            self._backend_url = 'unix://{:s}'.format(self._socket_path)

        else:
            proto = "https" if (tls or tls_verify) else "http"
            self._backend_url = '{:s}://{:s}:{:d}'.format(
                proto, self._endpoint, self._docker_port)

        self._tls = None
        if tls:
            self._tls = docker.tls.TLSConfig(
                    verify = tls_verify,
                    client_cert = (tls_cert, tls_key),
                    ca_cert = tls_ca_cert,
                    ssl_version = ssl_version)

        self._backend = docker.Client(
            base_url=self._backend_url,
            version=Ship.DEFAULT_DOCKER_VERSION,
            timeout=timeout or Ship.DEFAULT_DOCKER_TIMEOUT,
            tls=self._tls)

    @property
    def ip(self):
        """Returns this ship's IP address or hostname."""
        return self._ip

    @property
    def endpoint(self):
        """Returns this ship's Docker endpoint IP address or hostname."""
        return self._endpoint

    @property
    def backend(self):
        """Returns the Docker client wrapper to talk to the Docker daemon on
        this host."""
        return self._backend

    @property
    def address(self):
        if self._tunnel:
            return '{} (ssh:{})'.format(self.name, self._tunnel.bind_port)
        return self.name

    def get_image_ids(self):
        """Returns a dictionary of tagged images available on the Docker daemon
        running on this ship."""
        images = {}
        for image in self._backend.images():
            if image['RepoTags'] is '<none>:<none>':
                continue
            for tag in image['RepoTags']:
                images[tag] = image['Id']
        return images

    def __repr__(self):
        if self._tunnel:
            return '{}@{} via ssh://{}@{}:{}->{}'.format(
                self.name, self._ip, self._tunnel.ssh_user,
                self._endpoint, self._tunnel.bind_port, self._docker_port)
        return '{}@{} via {}'.format(self.name, self._ip, self._backend_url)


class Service(Entity):
    """A Service is a collection of Containers running on one or more Ships
    that constitutes a logical grouping of containers that make up an
    infrastructure service.

    Services may depend on each other. This dependency tree is honored when
    services need to be started.
    """

    def __init__(self, name, image, omit=True, schema=None, env=None):
        """Instantiate a new named service/component of the platform using a
        given Docker image.

        By default, a service has no dependencies. Dependencies are resolved
        and added once all Service objects have been instantiated.

        Args:
            name (string): the name of this service.
            image (string): the name of the Docker image the instances of this
                service should use.
            omit (boolean): Whether to include this service in no-argument
                commands or omit it.
            schema (dict): Maestro schema versioning information.
            env (dict): a dictionary of environment variables to use as the
                base environment for all instances of this service.
        """
        Entity.__init__(self, name)
        self._image = image
        self._omit = omit
        self._schema = schema
        self.env = env or {}
        self._requires = set([])
        self._wants_info = set([])
        self._needed_for = set([])
        self._containers = {}

    @property
    def image(self):
        return self._image

    @property
    def omit(self):
        return self._omit

    @property
    def dependencies(self):
        return self._requires

    @property
    def requires(self):
        """Returns the full set of direct and indirect dependencies of this
        service."""
        dependencies = self._requires
        for dep in dependencies:
            dependencies = dependencies.union(dep.requires)
        return dependencies

    @property
    def wants_info(self):
        """Returns the full set of "soft" dependencies this service wants
        information about through link environment variables."""
        return self._wants_info

    @property
    def needed_for(self):
        """Returns the full set of direct and indirect dependents (aka services
        that depend on this service)."""
        dependents = self._needed_for
        for dep in dependents:
            dependents = dependents.union(dep.needed_for)
        return dependents

    @property
    def containers(self):
        """Return an ordered list of instance containers for this service, by
        instance name."""
        return map(lambda c: self._containers[c],
                   sorted(self._containers.keys()))

    def add_dependency(self, service):
        """Declare that this service depends on the passed service."""
        self._requires.add(service)

    def add_dependent(self, service):
        """Declare that the passed service depends on this service."""
        self._needed_for.add(service)

    def add_wants_info(self, service):
        """Declare that this service wants information about the passed service
        via link environment variables."""
        self._wants_info.add(service)

    def register_container(self, container):
        """Register a new instance container as part of this service."""
        self._containers[container.name] = container

    def get_link_variables(self, add_internal=False):
        """Return the dictionary of all link variables from each container of
        this service. An additional variable, named '<service_name>_INSTANCES',
        contain the list of container/instance names of the service."""
        basename = re.sub(r'[^\w]', '_', self.name).upper()
        links = {}
        for c in self._containers.values():
            for name, value in c.get_link_variables(add_internal).items():
                links['{}_{}'.format(basename, name)] = value
        links['{}_INSTANCES'.format(basename)] = \
            ','.join(self._containers.keys())
        return links


class Container(Entity):
    """A Container represents an instance of a particular service that will be
    executed inside a Docker container on its target ship/host."""

    def __init__(self, name, ship, service, config=None, schema=None,
                 env_name='local'):
        """Create a new Container object.

        Args:
            name (string): the instance name (should be unique).
            ship (Ship): the Ship object representing the host this container
                is expected to be executed on.
            service (Service): the Service this container is an instance of.
            config (dict): the YAML-parsed dictionary containing this
                instance's configuration (ports, environment, volumes, etc.)
            schema (dict): Maestro schema versioning information.
            env_name (string): the name of the Maestro environment.
        """
        Entity.__init__(self, name)
        config = config or {}

        self._status = None  # The container's status, cached.
        self._ship = ship
        self._service = service
        self._image = config.get('image', service.image)
        self._schema = schema

        # Register this instance container as being part of its parent service.
        self._service.register_container(self)

        # Get command
        # TODO(mpetazzoni): remove deprecated 'cmd' support
        self.command = config.get('command', config.get('cmd'))

        # Parse the port specs.
        self.ports = self._parse_ports(config.get('ports', {}))

        # Get environment variables.
        self.env = dict(service.env)
        self.env.update(config.get('env', {}))

        def env_list_expand(elt):
            return type(elt) != list and elt \
                or ' '.join(map(env_list_expand, elt))

        for k, v in self.env.items():
            if type(v) == list:
                self.env[k] = env_list_expand(v)

        self.volumes = self._parse_volumes(config.get('volumes', {}))
        self.container_volumes = config.get('container_volumes', [])
        if type(self.container_volumes) != list:
            self.container_volumes = [self.container_volumes]
        self.container_volumes = set(self.container_volumes)

        # Check for conflicts
        for volume in self.volumes.values():
            if volume['bind'] in self.container_volumes:
                raise exceptions.InvalidVolumeConfigurationException(
                        'Conflict in {} between bind-mounted volume '
                        'and container-only volume on {}'
                        .format(self.name, volume['bind']))

        # Contains the list of containers from which volumes should be mounted
        # in this container. Host-locality and volume conflicts are checked by
        # the conductor.
        self.volumes_from = config.get('volumes_from', [])
        if type(self.volumes_from) != list:
            self.volumes_from = [self.volumes_from]
        self.volumes_from = set(self.volumes_from)

        # Get links
        self.links = dict(
            (name, alias) for name, alias in
            config.get('links', {}).items())

        # Should this container run with -privileged?
        self.privileged = config.get('privileged', False)

        # Network mode
        self.network_mode = config.get('net')
        self.pid_mode = config.get('pid')

        # Restart policy
        self.restart_policy = self._parse_restart_policy(config.get('restart'))

        # DNS settings for the container, always as a list
        self.dns = config.get('dns')
        if isinstance(self.dns, six.string_types):
            self.dns = [self.dns]

        # Stop timeout
        self.stop_timeout = config.get('stop_timeout', 10)

        # Get limits
        limits = config.get('limits', {})
        self.cpu_shares = limits.get('cpu')
        self.mem_limit = self._parse_bytes(limits.get('memory'))
        self.memswap_limit = self._parse_bytes(limits.get('swap'))

        # Work directory for the container
        self.workdir = config.get('workdir')

        # Seed the service name, container name and host address as part of the
        # container's environment.
        self.env.update({
            'MAESTRO_ENVIRONMENT_NAME': env_name,
            'SERVICE_NAME': self.service.name,
            'CONTAINER_NAME': self.name,
            'CONTAINER_HOST_ADDRESS': self.ship.ip,
            'DOCKER_IMAGE': self.image,
            'DOCKER_TAG': self.get_image_details()['tag'],
        })

        # With everything defined, build lifecycle state helpers as configured
        self._lifecycle = self._parse_lifecycle(config.get('lifecycle', {}))

    @property
    def ship(self):
        """Returns the Ship this container runs on."""
        return self._ship

    @property
    def service(self):
        """Returns the Service this container is an instance of."""
        return self._service

    @property
    def id(self):
        """Returns the ID of this container given by the Docker daemon, or None
        if the container doesn't exist."""
        status = self.status()
        return status and status.get('ID', status.get('Id', None))

    @property
    def shortid(self):
        """Returns a short representation of this container's ID, or '-' if the
        container is not running."""
        return self.id[:7] if self.id else '-'

    def is_running(self):
        """Refreshes the status of this container and tells if it's running or
        not."""
        status = self.status(refresh=True)
        return status and status['State']['Running']

    @property
    def image(self):
        """Return the full name and tag of the image used by instances of this
        service."""
        return self._image

    @property
    def short_image(self):
        """Return the abbreviated name (stripped of its registry component,
        when present) of the image used by this service."""
        return self._image[self._image.find('/')+1:]

    def get_image_details(self):
        """Return a dictionary detailing the image used by this service, with
        its repository name and the requested tag (defaulting to latest if not
        specified)."""
        p = self._image.rsplit(':', 1)
        if len(p) > 1 and '/' in p[1]:
            p[0] = self._image
            p.pop()
        return {'repository': p[0], 'tag': len(p) > 1 and p[1] or 'latest'}

    @property
    def shortid_and_tag(self):
        """Returns a string representing the tag of the image this container
        runs on and the short ID of the running container."""
        return '{}:{}'.format(self.get_image_details()['tag'], self.shortid)

    @property
    def started_at(self):
        """Returns the time at which the container was started."""
        status = self.status()
        return status and self._parse_go_time(status['State']['StartedAt'])

    @property
    def finished_at(self):
        """Returns the time at which the container finished executing."""
        status = self.status()
        return status and self._parse_go_time(status['State']['FinishedAt'])

    def status(self, refresh=False):
        """Retrieve the details about this container from the Docker daemon, or
        None if the container doesn't exist."""
        if refresh or not self._status:
            try:
                self._status = self.ship.backend.inspect_container(self.name)
            except APIError:
                pass

        return self._status

    def get_volumes(self):
        """Returns all the declared local volume targets within this container.
        This does not includes volumes from other containers."""
        volumes = set(self.container_volumes)
        for volume in self.volumes.values():
            volumes.add(volume['bind'])
        return volumes

    def get_link_variables(self, add_internal=False):
        """Build and return a dictionary of environment variables providing
        linking information to this container.

        Variables are named
        '<service_name>_<container_name>_{HOST,PORT,INTERNAL_PORT}'.
        """
        def _to_env_var_name(n):
            return re.sub(r'[^\w]', '_', n).upper()

        basename = _to_env_var_name(self.name)
        port_number = lambda p: p.split('/')[0]

        links = {'{}_HOST'.format(basename): self.ship.ip}
        for name, spec in self.ports.items():
            links['{}_{}_PORT'.format(basename, _to_env_var_name(name))] = \
                port_number(spec['external'][1])
            if add_internal:
                links['{}_{}_INTERNAL_PORT'.format(
                    basename, _to_env_var_name(name))] = \
                    port_number(spec['exposed'])
        return links

    def start_lifecycle_checks(self, state):
        """Check if a particular lifecycle state has been reached by executing
        all its defined checks. If not checks are defined, it is assumed the
        state is reached immediately."""

        if state not in self._lifecycle:
            # Return None to indicate no checks were performed.
            return None

        # HACK: Workaround for Python bug #10015 (also #14881). Fixed in
        # Python >= 2.7.5 and >= 3.3.2.
        thread = threading.current_thread()
        if not hasattr(thread, "_children"):
            thread._children = weakref.WeakKeyDictionary()

        pool = multiprocessing.pool.ThreadPool()
        return pool.map_async(lambda check: check.test(),
                              self._lifecycle[state])

    def ping_port(self, port):
        """Ping a single port, by its given name in the port mappings. Returns
        True if the port is opened and accepting connections, False
        otherwise."""
        parts = self.ports[port]['external'][1].split('/')
        if parts[1] == 'udp':
            return False

        return lifecycle.TCPPortPinger(self.ship.ip, int(parts[0]), 1).test()

    def _parse_bytes(self, s):
        if not s or not isinstance(s, six.string_types):
            return s

        units = {'k': 1024,
                 'm': 1024*1024,
                 'g': 1024*1024*1024}
        suffix = s[-1].lower()

        if suffix not in units.keys():
            if not s.isdigit():
                raise exceptions.EnvironmentConfigurationException(
                        'Unknown unit suffix {} in {} for container {}!'
                        .format(suffix, s, self.name))
            return int(s)

        return int(s[:-1]) * units[suffix]

    def _parse_restart_policy(self, spec):
        """Parse the restart policy configured for this container.

        Args:
            spec: the restart policy specification, as extract from the YAML.
            It can be a string <name>:<max-retries>, or a dictionary with the
            name and retries for the restart policy.
        Returns: A Docker-ready dictionary representing the parsed restart
            policy.
        """
        def _make_policy(name='no', retries=0):
            if name not in _VALID_RESTART_POLICIES:
                raise exceptions.InvalidRestartPolicyConfigurationException(
                        'Invalid restart policy {} for container {}; choose one of {}.'
                        .format(name, self.name, ', '.join(_VALID_RESTART_POLICIES)))
            return {'Name': name, 'MaximumRetryCount': int(retries)}

        try:
            if isinstance(spec, six.string_types):
                return _make_policy(*spec.split(':', 1))
            elif type(spec) == dict:
                return _make_policy(**spec)
        except exceptions.InvalidRestartPolicyConfigurationException as e:
            raise
        except:
            raise exceptions.InvalidRestartPolicyConfigurationException(
                    'Invalid restart policy format for container {}: "{}"'
                    .format(self.name, spec))

        # Fall-back to default
        return _make_policy()

    def _parse_volumes(self, volumes):
        """Parse the volume bindings defined by this container's configuration.

        Args:
            volumes (dict): the configured volume mappings as extracted from
                the YAML file.
        Returns: A dictionary of bindings host -> binding spec, where the
            binding spec specifies the target inside the container and its mode
            (read-only or read-write) in docker-py's format.
        """
        result = {}
        def _parse_spec(src, spec):
            # Short path for obsolete schemas
            # TODO(mpetazzoni): remove when obsoleted
            if self._schema and self._schema.get('schema') == 1:
                result[spec] = {'bind': src, 'ro': False}
                return

            if isinstance(spec, six.string_types):
                result[src] = {'bind': spec, 'ro': False}
            elif type(spec) == dict and 'target' in spec:
                result[src] = {'bind': spec['target'],
                               'ro': spec.get('mode', 'rw') == 'ro'}
            else:
                raise exceptions.InvalidVolumeConfigurationException(
                    'Invalid volume specification for container {}: {} -> {}'
                    .format(self.name, src, spec))

        for src, spec in volumes.items():
            _parse_spec(src, spec)
        return result

    def _parse_go_time(self, s):
        """Parse a time string found in the container status into a Python
        datetime object.

        Docker uses Go's Time.String() method to convert a UTC timestamp into a
        string, but that representation isn't directly parsable from Python as
        it includes nanoseconds: http://golang.org/pkg/time/#Time.String

        We don't really care about sub-second precision here anyway, so we
        strip it out and parse the datetime up to the second.

        Args:
            s (string): the time string from the container inspection
                dictionary.
        Returns: The corresponding Python datetime.datetime object, or None if
            the time string clearly represented a non-initialized time (which
            seems to be 0001-01-01T00:00:00Z in Go).
        """
        if not s:
            return None
        t = datetime.datetime.strptime(s.split('.')[0], '%Y-%m-%dT%H:%M:%S')
        return t if t.year > 1 else None

    def _parse_ports(self, ports):
        """Parse port mapping specifications for this container."""

        def validate_proto(port):
            parts = str(port).split('/')
            if len(parts) == 1:
                return '{:d}/tcp'.format(int(parts[0]))
            elif len(parts) == 2:
                try:
                    int(parts[0])
                    if parts[1] in ['tcp', 'udp']:
                        return port
                except ValueError:
                    pass
            raise exceptions.InvalidPortSpecException(
                ('Invalid port specification {}! ' +
                 'Expected format is <port> or <port>/{tcp,udp}.').format(
                    port))

        result = {}
        for name, spec in ports.items():
            # Single number, interpreted as being a TCP port number and to be
            # the same for the exposed port and external port bound on all
            # interfaces.
            if type(spec) == int:
                result[name] = {
                    'exposed': validate_proto(spec),
                    'external': ('0.0.0.0', validate_proto(spec)),
                }

            # Port spec is a string. This means either a protocol was specified
            # with /tcp or /udp, or that a mapping was provided, with each side
            # of the mapping optionally specifying the protocol.
            # External port is assumed to be bound on all interfaces as well.
            elif type(spec) == str:
                parts = list(map(validate_proto, spec.split(':')))
                if len(parts) == 1:
                    # If only one port number is provided, assumed external =
                    # exposed.
                    parts.append(parts[0])
                elif len(parts) > 2:
                    raise exceptions.InvalidPortSpecException(
                        ('Invalid port spec {} for port {} of {}! ' +
                         'Format should be "name: external:exposed".').format(
                            spec, name, self))

                if parts[0][-4:] != parts[1][-4:]:
                    raise exceptions.InvalidPortSpecException(
                        'Mismatched protocols between {} and {}!'.format(
                            parts[0], parts[1]))

                result[name] = {
                    'exposed': parts[0],
                    'external': ('0.0.0.0', parts[1]),
                }

            # Port spec is fully specified.
            elif type(spec) == dict and \
                    'exposed' in spec and 'external' in spec:
                spec['exposed'] = validate_proto(spec['exposed'])

                if type(spec['external']) != list:
                    spec['external'] = ('0.0.0.0', spec['external'])
                spec['external'] = (spec['external'][0],
                                    validate_proto(spec['external'][1]))

                result[name] = spec

            else:
                raise exceptions.InvalidPortSpecException(
                    'Invalid port spec {} for port {} of {}!'.format(
                        spec, name, self))

        return result

    def _parse_lifecycle(self, lifecycles):
        """Parse the lifecycle checks configured for this container and
        instantiate the corresponding check helpers, as configured."""
        return dict([
            (state, map(
                lambda c: (lifecycle.LifecycleHelperFactory
                           .from_config(self, c)),
                checks)) for state, checks in lifecycles.items()])

    def __repr__(self):
        return '{} (on {})'.format(self.name, self.ship.name)

    def __lt__(self, other):
        return self.name < other.name

    def __eq__(self, other):
        return self.name == other.name

    def __hash__(self):
        return hash(self.name)
