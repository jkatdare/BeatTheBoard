"""
Push the NJT credentials from .env into the Azure Container App as secrets and
wire them to the app's environment variables -- without typing the password
into a shell.

    python set_azure_secrets.py --dry-run   # parse-only: targets an app that
                                            # does not exist, so nothing is stored
    python set_azure_secrets.py             # for real

Why a script: on Windows `az` is az.cmd, so every argument passes through
cmd.exe, which treats a bare & | ^ as an operator and silently truncates the
value. Quoting rules differ between PowerShell, cmd and bash, and the error
`az` gives back never mentions any of this. Here the values are read from .env
and placed inside double quotes on the command line, which cmd.exe honours.
The two characters cmd.exe cannot be told to ignore even inside quotes are
" and %, so those are refused up front.
"""

import argparse
import os
import subprocess
import sys

import njt_logger as njt

APP, RG = "beattheboard", "beattheboard-rg"


def run(cmd, secret=""):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    out = (r.stdout + "\n" + r.stderr).strip()
    if secret:
        out = out.replace(secret, "***")
    return r.returncode, out


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="target a nonexistent app: proves parsing, stores nothing")
    args = ap.parse_args()

    njt.load_env()
    user = os.environ.get("NJT_USERNAME", "")
    pw = os.environ.get("NJT_PASSWORD", "")
    if not user or not pw:
        sys.exit("NJT_USERNAME / NJT_PASSWORD not found in .env")
    for bad in ('"', "%"):
        if bad in user or bad in pw:
            sys.exit("credential contains %r, which cannot pass through cmd.exe safely "
                     "-- add the secret in the Azure portal instead" % bad)

    app, rg = ("nope-app", "nope-rg") if args.dry_run else (APP, RG)
    print("target   : %s in %s%s" % (app, rg,
          "   (DRY RUN - this app does not exist; nothing will be stored)" if args.dry_run else ""))
    print("username : %s" % user)
    print("password : %d characters, read from .env, never printed" % len(pw))

    code, out = run('az containerapp secret set --name %s --resource-group %s '
                    '--secrets "njt-username=%s" "njt-password=%s"' % (app, rg, user, pw), pw)
    if args.dry_run and "does not exist" in out:
        status = "parsed OK  (fake app not found -- that is the intended outcome)"
    else:
        status = "ok" if code == 0 else "FAILED"
    print("\n[1] secret set : %s" % status)
    print("    " + out[:400].replace("\n", "\n    "))

    if args.dry_run:
        print("\nHow to read this: 'does not exist' means the secrets PARSED correctly and the")
        print("real run will work. A 'must be in format' error means they did not.")
        return
    if code != 0:
        sys.exit(1)

    code, out = run('az containerapp update --name %s --resource-group %s --set-env-vars '
                    'NJT_USERNAME=secretref:njt-username NJT_PASSWORD=secretref:njt-password'
                    % (app, rg), pw)
    print("\n[2] env vars   : %s" % ("ok" if code == 0 else "FAILED"))
    if code != 0:
        print("    " + out[:400].replace("\n", "\n    "))
        sys.exit(1)

    code, out = run('az containerapp secret list --name %s --resource-group %s -o table' % (app, rg))
    print("\n[3] secrets now on the app (names only):\n    " + out.replace("\n", "\n    "))

    code, out = run('az containerapp show --name %s --resource-group %s '
                    '--query properties.configuration.ingress.fqdn -o tsv' % (app, rg))
    if code == 0 and out:
        print("\nURL: https://%s" % out.strip())


if __name__ == "__main__":
    main()
