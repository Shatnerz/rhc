'''
The MIT License (MIT)

Copyright (c) 2013-2017 Robert H Chase

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
'''
from collections import namedtuple
import imp
from importlib import import_module
import os
import os.path
import traceback
import uuid

import rhc.config as config
from rhc.resthandler import LoggingRESTHandler, RESTMapper
from rhc.tcpsocket import SERVER, SSLParam
from rhc.timer import TIMERS

import logging
log = logging.getLogger(__name__)

'''
    Micro-service launcher

    Use a configuration file (named 'micro' in the current directory) to set up and run a REST
    service.

    The configuration file is composed of single-line directives with parameters that define the
    service. A simple example is this:

        PORT 12345
        ROUTE /ping?
            GET handle.ping

    This will listen on port 12345 for HTTP connections and route a GET on the exact url '/ping'
    to the ping function inside handle.py. The server operates an rhc.resthandler.RESTHandler.

    Directives:

        Directives can be any case and can be preceeded by white space, if desired. Any time a
        '#' is encoutered, it starts a comment, which is ignored.

        CONFIG name {default=<value>} {validate=int|bool|file} {env=<name>}

            Specify a single config file parameter.

            Note: CONFIG records define values which can be used to configure the service by
                  specifying configuration values in a file.  The config file is a file named 'config'
                  which has records that set or override values defined in 'micro'.  After all CONFIG
                  records are read from 'micro', the file 'config', if it exists, is loaded using
                  rhc.config.Config._load.

                  CONFIG (and CONFIG_SERVER) records must be specified before other micro directives.

                  See rhc.config.Config.

            The validate option will make sure that the value specified is valid according to these
            rules:

                int - must be an integer, or a string of digits; strings will be converted
                bool - must be True, False, or a string (any case) spelling TRUE or FALSE
                file - must be an existing file

            The env option will attempt to read the value from the environment variable.

            If an env is found, it takes precendence over other values. Values in the config
            file take precedence over default values. If no default value is specified, the
            value is None.

            If specified, the following configuration parameters control the operation of the
            main loop:

                loop.sleep - max number of milliseconds to sleep on socket poll, default 100
                loop.max_iterations - max polls per service loop, default 100

        CONFIG_SERVER <name> <port>

            Specify a SERVER config section.

            Generates default values for some of the config attributes mentioned under the
            SERVER directive below. For instance:

                CONFIG_SERVER private 10000

            will create a config section with:

                CONFIG private.port default=10000 validate=int
                CONFIG private.is_active default=True validate=bool
                CONFIG private.ssl.is_active default=False validate=bool
                CONFIG private.ssl.keyfile validate=file
                CONFIG private.ssl.certfile validate=file

            other SERVER values can still be explicitly specified, if desired.

        SETUP <path>

            Specify the path to a function to be run before entering the main loop. The function
            must accept an rhc.config.Config object as the first parameter.

        TEARDOWN <path>

            Specify the path to a function to be run after exiting the main loop.

        PORT <port>

            Specify a port on which to listen for HTTP connections. In order to to have more
            control over the listening port (for instance, HTTPS) use the SERVER directive.

            Multiple PORT directives can be specified.

        SERVER <name>

            Specify the section of a config file (see CONFIG) that defines a listening port's
            attributes.

            The following config attributes can be specified:

                port - port to listen on, required
                is_active - flag that enables/disables port, default=True
                handler - path to socket handler, default=rhc.micro.MicroRESTHandler
                          extend this handler, or one of the rhc.resthandlers for best results

                ssl.is_active - flag that enables/disables ssl, default=False
                ssl.keyfile - path to ssl keyfile
                ssl.certfile - path to ssl certfile

                http_max_content_length - self explanatory, default None (no enforced limit)
                http_max_line_length - max header line length, default 10000 bytes
                http_max_header_count - self explanatory, default 100
                hide_stack_trace - don't send stack trace to caller, default True

            Multiple SERVER directives can be specified.

        ROUTE <pattern>

           Specify a url pattern. This follows the rules of rhc.resthandler.RESTMapper.

        GET <path>
        POST <path>
        PUT <path>
        DELETE <path>

            Specify the path to a function to handle an HTTP method.

        IMPORT <path>

            import directives from another micro file. the imported file is interpreted
            as though it were inline.
'''


def _import(item_path, is_module=False):
    if is_module:
        return import_module(item_path)
    path, function = item_path.rsplit('.', 1)
    module = import_module(path)
    return getattr(module, function)


class MicroContext(object):

    def __init__(self, http_max_content_length, http_max_line_length, http_max_header_count, hide_stack_trace):
        self.http_max_content_length = http_max_content_length
        self.http_max_line_length = http_max_line_length
        self.http_max_header_count = http_max_header_count
        self.hide_stack_trace = hide_stack_trace


class MicroRESTHandler(LoggingRESTHandler):

    def __init__(self, socket, context):
        super(MicroRESTHandler, self).__init__(socket, context)
        context = context.context
        self.http_max_content_length = context.http_max_content_length
        self.http_max_line_length = context.http_max_line_length
        self.http_max_header_count = context.http_max_header_count
        self.hide_stack_trace = context.hide_stack_trace

    def on_rest_exception(self, exception_type, value, trace):
        code = uuid.uuid4().hex
        log.exception('exception encountered, code: %s', code)
        if self.hide_stack_trace:
            return 'oh, no! something broke. sorry about that.\nplease report this problem using the following id: %s\n' % code
        return traceback.format_exc(trace)


class Route(object):

    def __init__(self, pattern):
        self.pattern = pattern
        self.method = dict()

    def add(self, method, path):
        self.method[method] = _import(path)


class Server(object):

    def __init__(self, name, config):
        self.name = name
        context = MicroContext(
            config.http_max_content_length if hasattr(config, 'http_max_content_length') else None,
            config.http_max_line_length if hasattr(config, 'http_max_line_length') else 10000,
            config.http_max_header_count if hasattr(config, 'http_max_header_count') else 100,
            config.hide_stack_trace if hasattr(config, 'hide_stack_trace') else True,
        )
        self.mapper = RESTMapper(context)
        self.is_active = config.is_active if hasattr(config, 'is_active') else True
        self.port = int(config.port)
        self.handler = _import(config.handler, is_module=True) if hasattr(config, 'handler') else MicroRESTHandler
        self.ssl = None
        if hasattr(config, 'ssl') and config.ssl.is_active:
            self.ssl = SSLParam(
                server_side=True,
                keyfile=config.ssl.keyfile,
                certfile=config.ssl.certfile,
            )

    def add_route(self, pattern):
        if hasattr(self, 'route'):
            self.mapper.add(self.route.pattern, **self.route.method)
        self.route = Route(pattern)

    def add_method(self, method, path):
        self.route.add(method, path)

    def done(self):
        if self.is_active:
            if hasattr(self, 'route'):
                self.mapper.add(self.route.pattern, **self.route.method)
            try:
                SERVER.add_server(self.port, self.handler, self.mapper, ssl=self.ssl)
            except Exception:
                log.error('unable to add %s server on port %d', self.name, self.port)
                raise
            log.info('listening on %s port %d', self.name, self.port)


def _config_server(cfg, line):
    name, port = line.split()
    cfg._define(name + '.port', value=int(port), validator=config.validate_int)
    cfg._define(name + '.is_active', value=True, validator=config.validate_bool)
    cfg._define(name + '.ssl.is_active', value=False, validator=config.validate_bool)
    cfg._define(name + '.ssl.keyfile', validator=config.validate_file)
    cfg._define(name + '.ssl.certfile', validator=config.validate_file)
    return (
        name + '.port',
        name + '.is_active',
        name + '.ssl.is_active',
        name + '.ssl.keyfile',
        name + '.ssl.certfile',
    )


def _config(cfg, line):
    line = line.split(' ', 1)
    if len(line) == 2:
        name, line = line
        kwargs = {n: v for n, v in (t.split('=', 1) for t in line.split())}
        if 'validate' in kwargs:
            if kwargs['validate'] == 'int':
                f = config.validate_int
            elif kwargs['validate'] == 'bool':
                f = config.validate_bool
            elif kwargs['validate'] == 'file':
                f = config.validate_file
            else:
                raise Exception('%s is not a recognized validation type')
            del kwargs['validate']
            kwargs['validator'] = f
        if 'default' in kwargs:
            kwargs['value'] = kwargs['default']
            del kwargs['default']
            if 'validator' in kwargs:
                kwargs['value'] = kwargs['validator'](kwargs['value'])
    else:
        name = line[0]
        kwargs = {}
    cfg._define(name, **kwargs)
    return name


class FSM(object):

    def __init__(self):
        self.state = self.state_init
        self.teardown = lambda *x: None
        self.config = config.Config()
        self.config_keys = []
        self.ports = {}

    def handle(self, event, data, fname, linenum):
        self.data = data
        try:
            self.state(event)
        except Exception as e:
            raise Exception('%s, line=%d of %s' % (e, linenum, fname))

    def state_init(self, event):
        if event in ('config', 'config_server'):
            self.state = self.state_config  # switch states before processing event
            self.state(event)
        elif event == 'setup':
            path = os.getenv('MICRO_SETUP', self.data)
            _import(path)(self.config)
        elif event == 'teardown':
            self.teardown = _import(self.data)
        elif event == 'server':
            self.server = Server(self.data, getattr(self.config, self.data))
            if self.server.port in self.ports:
                raise Exception('port %s already defined for server "%s"' % (self.server.port, self.ports[self.server.port]))
            self.ports[self.server.port] = self.server.name
            self.state = self.state_server
        elif event == 'port':
            self.server = Server('default', namedtuple('config', 'port')(int(self.data)))
            self.state = self.state_server
        elif event == 'done':
            self.state = self.state_done
        else:
            raise Exception('invalid record ' + event)

    def state_config(self, event):
        if event == 'config':
            self.config_keys.append(_config(self.config, self.data))
        elif event == 'config_server':
            self.config_keys.extend(_config_server(self.config, self.data))
        else:
            if os.path.exists('config'):
                self.config._load('config')  # process config file
            self.state = self.state_init     # switch states before processing event
            self.state(event)

    def state_server(self, event):
        if event == 'route':
            self.server.add_route(self.data)
            self.state = self.state_route
        elif event == 'done':
            self.server.done()
            self.state = self.state_done
        else:
            raise Exception('invalid record ' + event)

    def state_route(self, event):
        if event in ('get', 'post', 'put', 'delete'):
            self.server.add_method(event, self.data)
        elif event == 'route':
            self.server.add_route(self.data)
        elif event == 'server':
            self.server.done()
            self.server = Server(self.data, getattr(self.config, self.data))
            if self.server.port in self.ports:
                raise Exception('port %s already defined for server "%s"' % (self.server.port, self.ports[self.server.port]))
            self.ports[self.server.port] = self.server.name
            self.state = self.state_server
        elif event == 'port':
            self.server.done()
            self.server = Server('default', namedtuple('config', 'port')(int(self.data)))
            self.state = self.state_server
        elif event == 'done':
            self.server.done()
            self.state = self.state_done
        else:
            raise Exception('invalid record ' + event)

    def state_done(self, event):
        raise Exception('unexpected event ' + event)


def _load(fname, files=None, lines=None):
    """Recursively load files."""
    if files is None:
        files = []
    if lines is None:
        lines = []

    if fname in files:
        raise Exception('a micro file (in this case, %s) cannot be recursively imported' % fname)
    files.append(fname)
    dir_path = os.path.dirname(fname)

    for n, l in enumerate(open(fname).readlines(), start=1):
        ll = l.split()
        if len(ll) > 1 and ll[0].lower() == 'import':
            import_fname = ' '.join(ll[1:])
            if '.' in import_fname and os.path.sep not in import_fname:  # path is dot separated
                parts = import_fname.split('.')
                sink, path, sink = imp.find_module(parts[0])  # use module-based location
                import_fname = os.path.join(path, *parts[1:])
            elif not import_fname.startswith(os.path.sep):  # path is relative
                import_fname = os.path.join(dir_path, import_fname)
            _load(import_fname, files, lines)
        else:
            lines.append((fname, n, l))
    return lines


def parse(s, config_only=False):

    lines = _load(s)

    fsm = FSM()

    for fname, lnum, l in lines:
        l = l.split('#', 1)[0].strip()
        if not l:
            continue
        try:
            n, v = l.split(' ', 1)
            n = n.lower()
        except ValueError as e:
            log.warning('parse error on line %d of %s: %s', lnum, fname, e.message)
            raise
        if config_only and n not in ('config', 'config_server'):
            continue
        fsm.handle(n, v, fname, lnum)
    fsm.handle('done', None, fname, lnum + 1)

    if config_only:
        return namedtuple('config', 'config keys')(fsm.config, fsm.config_keys)

    sleep = 100
    max_iterations = 100
    if hasattr(fsm, 'config'):
        if hasattr(fsm.config, 'loop'):
            sleep = fsm.config.loop.sleep
            max_iterations = fsm.config.loop.max_iterations

    return namedtuple('control', 'sleep, max_iterations, teardown')(sleep, max_iterations, fsm.teardown)


def run(sleep=100, max_iterations=100):
    while True:
        try:
            SERVER.service(delay=sleep/1000.0, max_iterations=max_iterations)
            TIMERS.service()
        except KeyboardInterrupt:
            log.info('Received shutdown command from keyboard')
            break
        except Exception:
            log.exception('exception encountered')


def load_config():
    return parse('micro', config_only=True).config


if __name__ == '__main__':
    import sys

    logging.basicConfig(level=logging.DEBUG)

    if len(sys.argv) > 1 and sys.argv[1] == 'config':
        cfg = parse('micro', config_only=True)
        if len(sys.argv) > 2:
            v = cfg.config._get(sys.argv[2])
            print v if v else ''
        else:
            for k in cfg.keys:
                v = getattr(cfg.config, k)
                print '%s=%s' % (k, v if v is not None else '')
    else:
        try:
            control = parse('micro')
        except Exception as e:
            control = None
            print e
        if control:
            run(control.sleep, control.max_iterations)
            control.teardown()
