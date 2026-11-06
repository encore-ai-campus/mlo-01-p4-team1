"""Build a self-contained Python SSM script from trusted, repository-owned sources."""
import base64
from pathlib import Path

def dispatcher_script():
    here = Path(__file__).resolve().parent
    root = here.parents[2]
    verifier = root/'aws-infra/contracts/artifact_verifier.py'
    source = verifier.read_bytes()
    dispatcher = (here/'dispatcher.py').read_bytes()
    # The first two modules contain no inputs from the artifact or SSM caller.
    return ["set -eu", "umask 077", "python3 - '{{ Operation }}' <<'P4_TRUSTED_DISPATCH'",
            'import base64, json, sys, types',
            'verifier = types.ModuleType("artifact_verifier")',
            'sys.modules["artifact_verifier"] = verifier',
            'exec(compile(base64.b64decode('+repr(base64.b64encode(source).decode())+'), "trusted_artifact_verifier.py", "exec"), verifier.__dict__)',
            'exec(compile(base64.b64decode('+repr(base64.b64encode(dispatcher).decode())+'), "trusted_dispatcher.py", "exec"))',
            "config = json.loads(r'''${jsonencode({environment=var.binding.environment,generation=var.binding.generation,bucket=var.binding.bucket_names.transfer,artifact=var.binding.artifact,instances={for name,n in local.nodes:aws_instance.host[name].id=>{name=name,role=n.role}}})}''')",
            'dispatch(config, sys.argv[1])', 'P4_TRUSTED_DISPATCH']
