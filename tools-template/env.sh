# Managed by `agent setup`; changes here are reset on the next setup. Put your own
# settings in /tools/env.d/<tool>.sh instead.
#
# Bash reads this before every command (BASH_ENV), so a snippet added to env.d/
# works on the next command. PATH is the exception: Claude Code fixes PATH when a
# chat starts, so programs go in /tools/bin (already on PATH) as a file or a small
# launcher, not as a PATH change in env.d/.
#
# Keep it silent and quick, and safe to read more than once.

export AGENT_TOOLS="${AGENT_TOOLS:-/tools}"

# The active Java, if any: point /tools/jdks/current at the JDK you want.
if [ -x "$AGENT_TOOLS/jdks/current/bin/java" ]; then
    export JAVA_HOME="$AGENT_TOOLS/jdks/current"
fi

for _agent_tools_env in "$AGENT_TOOLS"/env.d/*.sh; do
    [ -r "$_agent_tools_env" ] && . "$_agent_tools_env"
done
unset _agent_tools_env
