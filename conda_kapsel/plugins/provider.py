# -*- coding: utf-8 -*-
# ----------------------------------------------------------------------------
# Copyright © 2016, Continuum Analytics, Inc. All rights reserved.
#
# The full license is in the file LICENSE.txt, distributed with this software.
# ----------------------------------------------------------------------------
"""Types related to project requirement providers."""
from __future__ import absolute_import

from abc import ABCMeta, abstractmethod
from copy import deepcopy
import os
import shutil

from conda_kapsel.internal import conda_api
from conda_kapsel.internal import logged_subprocess
from conda_kapsel.internal.metaclass import with_metaclass
from conda_kapsel.internal.makedirs import makedirs_ok_if_exists
from conda_kapsel.internal.simple_status import SimpleStatus


def _service_directory(local_state_file, relative_name):
    return os.path.join(os.path.dirname(local_state_file.filename), "services", relative_name)


class ProvideContext(object):
    """A context passed to ``Provider.provide()`` representing state that can be modified."""

    def __init__(self, environ, local_state_file, default_env_spec_name, status, mode):
        """Create a ProvideContext.

        Args:
            environ (dict): environment variables to be read and modified
            local_state_file (LocalStateFile): to store any created state
            status (RequirementStatus): current status
            mode (str): one of PROVIDE_MODE_PRODUCTION, PROVIDE_MODE_DEVELOPMENT, PROVIDE_MODE_CHECK
        """
        self.environ = environ
        self._local_state_file = local_state_file
        self._default_env_spec_name = default_env_spec_name
        self._status = status
        self._mode = mode

    def ensure_service_directory(self, relative_name):
        """Create a directory in PROJECT_DIR/services with the given name.

        The name should be unique to the ServiceRequirement creating the directory,
        so usually the requirement's env var.

        Args:
            relative_name (str): name to distinguish this dir from other service directories
        """
        path = _service_directory(self._local_state_file, relative_name)
        makedirs_ok_if_exists(path)
        return path

    def transform_service_run_state(self, service_name, func):
        """Run a function which takes and potentially modifies the state of a service.

        If the function modifies the state it's given, the new state will be saved
        and passed in next time.

        Args:
            service_name (str): the name of the service, should be
                specific enough to uniquely identify the provider
            func (function): function to run, passing it the current state

        Returns:
            Whatever ``func`` returns.
        """
        old_state = self._local_state_file.get_service_run_state(service_name)
        modified = deepcopy(old_state)
        result = func(modified)
        if modified != old_state:
            self._local_state_file.set_service_run_state(service_name, modified)
            self._local_state_file.save()
        return result

    @property
    def status(self):
        """Get the current ``RequirementStatus``."""
        return self._status

    @property
    def local_state_file(self):
        """Get the LocalStateFile."""
        return self._local_state_file

    @property
    def default_env_spec_name(self):
        """Get the default env spec."""
        return self._default_env_spec_name

    @property
    def mode(self):
        """Get flavor of provide.

        Value should be ``PROVIDE_MODE_DEVELOPMENT``, ``PROVIDE_MODE_PRODUCTION``, or ``PROVIDE_MODE_CHECK``.
        """
        return self._mode


def shutdown_service_run_state(local_state_file, service_name):
    """Run any shutdown commands from the local state file for the given service.

    Also remove the shutdown commands from the file.

    Args:
        local_state_file (LocalStateFile): local state
        service_name (str): the name of the service, usually a
            variable name, should be specific enough to uniquely
            identify the provider

    Returns:
        a `Status` instance potentially containing errors
    """
    run_states = local_state_file.get_all_service_run_states()
    if service_name not in run_states:
        return SimpleStatus(success=True, description=("Nothing to do to shut down %s." % service_name))

    errors = []
    state = run_states[service_name]
    if 'shutdown_commands' in state:
        commands = state['shutdown_commands']
        for command in commands:
            code = logged_subprocess.call(command)
            if code != 0:
                errors.append("Shutting down %s, command %s failed with code %d." % (service_name, repr(command), code))
    # clear out the run state once we try to shut it down
    local_state_file.set_service_run_state(service_name, dict())
    local_state_file.save()

    if errors:
        return SimpleStatus(success=False,
                            description=("Shutdown commands failed for %s." % service_name),
                            errors=errors)
    else:
        return SimpleStatus(success=True, description=("Successfully shut down %s." % service_name))


def delete_service_directory(local_state_file, relative_name):
    """Delete a directory in PROJECT_DIR/services with the given name.

    The name should be unique to the ServiceRequirement creating the directory,
    so usually the requirement's env var.

    IF this fails, it does so silently (returns no errors).

    Args:
        relative_name (str): name to distinguish this dir from other service directories

    Returns:
        None
    """
    path = _service_directory(local_state_file, relative_name)
    try:
        shutil.rmtree(path=path)
    except OSError:
        pass
    # also delete the services directory itself, if it's now empty
    try:
        # this fails on non-empty dir
        os.rmdir(os.path.dirname(path))
    except OSError:
        pass


class ProviderAnalysis(object):
    """A Provider's preflight check snapshotting the state prior to ``provide()``.

    Instances of this class are immutable, and are usually created as part of a
    ``RequirementStatus``.
    """

    def __init__(self, config, missing_env_vars_to_configure, missing_env_vars_to_provide):
        """Create a ProviderAnalysis."""
        self._config = deepcopy(config)  # defensive copy so we don't modify the original
        self._missing_env_vars_to_configure = missing_env_vars_to_configure
        self._missing_env_vars_to_provide = missing_env_vars_to_provide

    @property
    def config(self):
        """Get the configuration dict from the time of analysis."""
        return self._config

    @property
    def missing_env_vars_to_configure(self):
        """Get the env vars we were missing in order to configure, from the time of analysis."""
        return self._missing_env_vars_to_configure

    @property
    def missing_env_vars_to_provide(self):
        """Get the env vars we were missing in order to provide, from the time of analysis."""
        return self._missing_env_vars_to_provide


class ProvideResult(object):
    """A Provider's results from the ``provide()`` call.

    Instances of this class are immutable, and are returned from ``provide()``.
    """

    def __init__(self, errors=None, logs=None):
        """Create a ProvideResult."""
        if errors is None:
            errors = []
        if logs is None:
            logs = []
        self._errors = errors
        self._logs = logs

    def copy_with_additions(self, errors=None, logs=None):
        """Copy this result, appending additional errors and logs."""
        if errors is None:
            errors = []
        if logs is None:
            logs = []
        if len(errors) == 0 and len(logs) == 0:
            # we don't have to actually copy since we are immutable
            return self
        else:
            return ProvideResult(errors=(self._errors + errors), logs=(self._logs + logs))

    @property
    def errors(self):
        """Get any fatal errors that occurred during provide() preventing success."""
        return self._errors

    @property
    def logs(self):
        """Get any debug logs that occurred during provide()."""
        return self._logs

    @classmethod
    def empty(cls):
        """Get an empty ProvideResult (currently a singleton since these are immutable)."""
        return _emptyProvideResult

# get this via ProvideResult.empty()
_emptyProvideResult = ProvideResult()


class Provider(with_metaclass(ABCMeta)):
    """A Provider can take some action to meet a Requirement."""

    @abstractmethod
    def missing_env_vars_to_configure(self, requirement, environ, local_state_file):
        """Get a list of unset environment variable names that must be set before configuring this provider.

        Args:
            requirement (Requirement): requirement instance we are providing for
            environ (dict): current environment variable dict
            local_state_file (LocalStateFile): local state file
        """
        pass  # pragma: no cover

    @abstractmethod
    def missing_env_vars_to_provide(self, requirement, environ, local_state_file):
        """Get a list of unset environment variable names that must be set before calling provide().

        Args:
            requirement (Requirement): requirement instance we are providing for
            environ (dict): current environment variable dict
            local_state_file (LocalStateFile): local state file
        """
        pass  # pragma: no cover

    @abstractmethod
    def read_config(self, requirement, environ, local_state_file, default_env_spec_name, overrides):
        """Read a config dict from the local state file for the given requirement.

        Args:
            requirement (Requirement): the requirement we're providing
            environ (dict): current environment variables
            local_state_file (LocalStateFile): file to read from
            default_env_spec_name (str): the fallback env spec name
            overrides (UserConfigOverrides): user-supplied forced config
        """
        pass  # pragma: no cover

    def set_config_values_as_strings(self, requirement, environ, local_state_file, default_env_spec_name, overrides,
                                     values):
        """Set some config values in the state file (should not save the file).

        Args:
            requirement (Requirement): the requirement we're providing
            environ (dict): current environment variables
            local_state_file (LocalStateFile): file to save to
            default_env_spec_name (str): default env spec name for this prepare
            overrides (UserConfigOverrides): if any values in here change, delete the override
            values (dict): dict from string to string
        """
        pass  # silently ignore unknown config values

    def config_html(self, requirement, environ, local_state_file, overrides, status):
        """Get an HTML string for configuring the provider.

        The HTML string must contain a single <form> tag. Any
        <input>, <textarea>, and <select> elements should have
        their name attribute set to match the dict keys used in
        ``read_config()``'s result.  The <form> should not have a
        submit button, since it will be merged with other
        forms. The initial state of all the form fields will be
        auto-populated from the values in ``read_config()``.  When
        the form is submitted, any changes made by the user will
        be set back using ``set_config_values_as_strings()``.

        This is simple to use, but for now not very flexible; if you need
        more flexibility let us know and we can figure out what API
        to add in future versions.

        Args:
            requirement (Requirement): the requirement we're providing
            environ (dict): current environment variables
            local_state_file (LocalStateFile): file to save to
            status (RequirementStatus): last-computed status

        Returns:
            An HTML string or None if there's nothing to configure.

        """
        return None

    def analyze(self, requirement, environ, local_state_file, default_env_spec_name, overrides):
        """Analyze whether and how we'll be able to provide the requirement.

        This is used to show the situation in the UI, and also to
        consolidate all IO-type work in one place (inside
        Requirement.check_status()).

        Returns:
          A ``ProviderAnalysis`` instance.
        """
        config = self.read_config(requirement, environ, local_state_file, default_env_spec_name, overrides)
        missing_to_configure = self.missing_env_vars_to_configure(requirement, environ, local_state_file)
        missing_to_provide = self.missing_env_vars_to_provide(requirement, environ, local_state_file)
        return ProviderAnalysis(config=config,
                                missing_env_vars_to_configure=missing_to_configure,
                                missing_env_vars_to_provide=missing_to_provide)

    @abstractmethod
    def provide(self, requirement, context):
        """Execute the provider, fulfilling the requirement.

        The implementation should read and modify the passed-in
        ``environ`` rather than accessing the OS environment
        directly.

        Args:
            requirement (Requirement): requirement we want to meet
            context (ProvideContext): context containing project state

        Returns:
            a ``ProvideResult`` instance

        """
        pass  # pragma: no cover

    @abstractmethod
    def unprovide(self, requirement, environ, local_state_file, overrides, requirement_status=None):
        """Undo the provide, cleaning up any files or processes we created.

        The requirement may still be met after this, if our providing wasn't
        really needed.

        Args:
            requirement (Requirement): requirement we want to de-provide
            environ (dict): current env vars, often from a previous prepare
            local_state_file (LocalStateFile): the local state
            overrides (UserConfigOverrides): overrides to state
            requirement_status (RequirementStatus or None): requirement status if available

        Returns:
            a `Status` instance describing the (non)success of the unprovision
        """
        pass  # pragma: no cover


class EnvVarProvider(Provider):
    """Meets a requirement for an env var by letting people set it manually."""

    def _local_state_override(self, requirement, local_state_file):
        return local_state_file.get_value(["variables", requirement.env_var], default=None)

    def _disabled_local_state_override(self, requirement, local_state_file):
        return local_state_file.get_value(["disabled_variables", requirement.env_var], default=None)

    def missing_env_vars_to_configure(self, requirement, environ, local_state_file):
        """Override superclass to require env prefix."""
        if self._get_env_prefix(environ) is not None:
            return ()
        else:
            return (conda_api.conda_prefix_variable(), )

    def missing_env_vars_to_provide(self, requirement, environ, local_state_file):
        """Override superclass to require env prefix."""
        return self.missing_env_vars_to_configure(requirement, environ, local_state_file)

    def _get_env_prefix(self, environ):
        # on unix, ENV_PATH is the prefix and DEFAULT_ENV can be just a name,
        # on windows DEFAULT_ENV is always the prefix
        return environ.get(conda_api.conda_prefix_variable(), None)

    def read_config(self, requirement, environ, local_state_file, default_env_spec_name, overrides):
        """Override superclass to read env var value."""
        config = dict()
        value = None
        if requirement.encrypted:
            # import keyring locally because it's an optional dependency
            # that prints a warning when it's needed but not found.
            import conda_kapsel.internal.keyring as keyring

            env_prefix = self._get_env_prefix(environ)
            if env_prefix is None:
                value = None
            else:
                value = keyring.get(env_prefix, requirement.env_var)

        # note that we will READ an encrypted value from local
        # state if someone puts it in there by hand, but we won't
        # ever write one there ourselves.
        if value is None:
            value = self._local_state_override(requirement, local_state_file)

        disabled_value = self._disabled_local_state_override(requirement, local_state_file)
        was_disabled = value is None and disabled_value is not None
        if was_disabled:
            value = disabled_value

        if value is not None:
            config['value'] = value

        if value is not None and not was_disabled:
            source = 'variables'
        elif requirement.env_var in environ:
            source = 'environ'
            config['value'] = environ[requirement.env_var]
        elif 'default' in requirement.options:
            source = 'default'
        else:
            source = 'unset'
        config['source'] = source
        return config

    def set_config_values_as_strings(self, requirement, environ, local_state_file, default_env_spec_name, overrides,
                                     values):
        """Override superclass to set env var value."""
        if requirement.encrypted:
            self._set_encrypted_config_values_as_strings(requirement, environ, local_state_file, default_env_spec_name,
                                                         overrides, values)
        else:
            self._set_nonencrypted_config_values_as_strings(requirement, environ, local_state_file,
                                                            default_env_spec_name, overrides, values)

    def _set_nonencrypted_config_values_as_strings(self, requirement, environ, local_state_file, default_env_spec_name,
                                                   overrides, values):
        override_path = ["variables", requirement.env_var]
        disabled_path = ["disabled_variables", requirement.env_var]

        # we set override_path only if the source is variables,
        # otherwise the value goes in disabled_variables. If we
        # don't have a source that means the only option we
        # presented was to set the local override, so default to
        # 'variables'
        overriding = (values.get('source', 'variables') == 'variables')

        # If there's an existing override value and the source is not 'variables',
        # we need to be sure to move the existing to disabled_variables.
        # Also, we save values['value'] from the web form in local_state_file,
        # even if we aren't using it as the source right now.

        local_override_value = self._local_state_override(requirement, local_state_file)
        if local_override_value is None:
            local_override_value = self._disabled_local_state_override(requirement, local_state_file)

        value_string = values.get('value', local_override_value)

        if value_string is not None:
            if value_string == '':
                # the reason empty string unsets is that otherwise there's no easy
                # way to unset from a web form
                local_state_file.unset_value(override_path)
                local_state_file.unset_value(disabled_path)
            else:
                if overriding:
                    local_state_file.set_value(override_path, value_string)
                    local_state_file.unset_value(disabled_path)
                else:
                    local_state_file.set_value(disabled_path, value_string)
                    local_state_file.unset_value(override_path)

    def _set_encrypted_config_values_as_strings(self, requirement, environ, local_state_file, default_env_spec_name,
                                                overrides, values):
        # import keyring locally because it's an optional dependency
        # that prints a warning when it's needed but not found.
        import conda_kapsel.internal.keyring as keyring

        env_prefix = self._get_env_prefix(environ)
        from_keyring = keyring.get(env_prefix, requirement.env_var)
        value_string = values.get('value', from_keyring)

        if value_string is not None:
            if value_string == '':
                keyring.unset(env_prefix, requirement.env_var)
            else:
                keyring.set(env_prefix, requirement.env_var, value_string)

    def _extra_source_options_html(self, requirement, environ, local_state_file, status):
        """Override this in a subtype to add choices to the config HTML.

        Choices should be radio inputs with name="source"
        """
        return ""

    def config_html(self, requirement, environ, local_state_file, overrides, status):
        """Override superclass to provide our config html."""
        if status.requirement.encrypted:
            input_type = 'password'
        else:
            input_type = 'text'

        extra_html = self._extra_source_options_html(requirement, environ, local_state_file, status)

        choices_html = extra_html

        if requirement.env_var in environ:
            choices_html = choices_html + """
            <div>
              <label><input type="radio" name="source" value="environ"/>Keep value '{from_environ}'</label>
            </div>
            <div>
              <label><input type="radio" name="source" value="variables"/>Use this value instead:
                     <input type="{input_type}" name="value"/></label>
            </div>
            """.format(from_environ=environ[requirement.env_var],
                       input_type=input_type)
        else:
            if 'default' in requirement.options:
                choices_html = choices_html + """
                <div>
                  <label><input type="radio" name="source" value="default"/>Keep default '{from_default}'</label>
                </div>
                <div>
                  <label><input type="radio" name="source" value="variables"/>Use this value instead:
                         <input type="{input_type}" name="value"/></label>
                </div>
                """.format(input_type=input_type,
                           from_default=requirement.options['default'])
            else:
                choices_html = choices_html + """
                <div>
                  <label><input type="radio" name="source" value="variables"/>Use this value:
                         <input type="{input_type}" name="value"/></label>
                </div>
                """.format(input_type=input_type)

        # print(("%s: choices_html=\n" % self.__class__.__name__) + choices_html)

        return """
<form>
  %s
</form>
""" % (choices_html)

    def provide(self, requirement, context):
        """Override superclass to use configured env var (or already-set env var)."""
        errors = []
        logs = []

        # We prefer the values in this order:
        #  - value set in project-local state overrides everything
        #    (otherwise the UI for configuring the value would end
        #    up ignored)
        #  - value in the keyring overrides (treated the same as
        #    kapsel-local.yml, but for encrypted variables)
        #  - then anything already set in the environment wins, so you
        #    can override on the command line like `FOO=bar myapp`
        #  - then the kapsel.yml default value
        local_state_override = None
        if requirement.encrypted:
            # import keyring locally because it's an optional dependency
            # that prints a warning when it's needed but not found.
            import conda_kapsel.internal.keyring as keyring

            env_prefix = self._get_env_prefix(context.environ)
            if env_prefix is not None:
                local_state_override = keyring.get(env_prefix, requirement.env_var)

        # we will read encrypted vars from local state, though we never
        # put them in there ourselves
        if local_state_override is None:
            local_state_override = self._local_state_override(requirement, context.local_state_file)

        if local_state_override is not None:
            # kapsel-local.yml
            #
            # variables:
            #   REDIS_URL: "redis://example.com:1234"
            context.environ[requirement.env_var] = local_state_override
        elif requirement.env_var in context.environ:
            # nothing to do here
            pass
        elif 'default' in requirement.options:
            # kapsel.yml
            #
            # variables:
            #   REDIS_URL:
            #     default: "redis://example.com:1234"
            value = requirement.options['default']
            if value is not None:
                context.environ[requirement.env_var] = value
        else:
            pass

        return ProvideResult.empty().copy_with_additions(errors, logs)

    def unprovide(self, requirement, environ, local_state_file, overrides, requirement_status=None):
        """Override superclass to return success always."""
        return SimpleStatus(success=True, description=("Nothing to clean up for %s." % requirement.env_var))
