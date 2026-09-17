"""
Give the container a permanent place to keep the scorecard.

    python setup_storage.py            # dry run: show the plan and the YAML
    python setup_storage.py --apply    # actually do it

A Container Apps filesystem is ephemeral: every deploy starts a fresh container,
so a scorecard written inside it resets on each push. This mounts a small Azure
Files share at /data and points two things at it:

    STATS_DB          /data/stats.db          the scorecard
    NJT_TOKEN_CACHE   /data/.njt_token.json   the API token

The token matters as much as the scorecard. NJ Transit allows ~10 token mints
per day; with the cache inside the container, every restart burned one. On the
share it survives restarts and deploys.

Cost is a few cents a month for a 1 GiB share.

Mounting a volume is the one thing `az containerapp update` cannot do with
flags, so this generates the YAML from the app's live settings rather than
hand-writing it -- that way the image, secrets and scale rules come back
exactly as they are now.
"""

import argparse
import json
import subprocess
import sys

RG = "beattheboard-rg"
APP = "beattheboard"
ENV = "beattheboard-env"
SHARE = "data"                 # name of both the file share and the env storage link
MOUNT_PATH = "/data"
YAML_PATH = "containerapp.yaml"

EXTRA_ENV = [
    ("STATS_DB", MOUNT_PATH + "/stats.db"),
    ("NJT_TOKEN_CACHE", MOUNT_PATH + "/.njt_token.json"),
]


def az(args, capture=True, check=True):
    cmd = "az " + args
    r = subprocess.run(cmd, shell=True, capture_output=capture, text=True)
    if check and r.returncode != 0:
        sys.exit("FAILED: " + cmd + "\n" + (r.stderr or r.stdout)[:600])
    return (r.stdout or "").strip()


def az_json(args):
    out = az(args)
    return json.loads(out) if out else None


def storage_account_name():
    """3-24 lowercase alphanumerics, globally unique. Derived from the
    subscription so re-running picks the same name instead of making a second."""
    sub = az("account show --query id -o tsv")
    return ("bb" + "".join(c for c in sub if c.isalnum()).lower())[:16] + "store"


def build_yaml(cfg):
    env = []
    for e in cfg.get("env") or []:
        env.append((e["name"], e.get("secretRef"), e.get("value")))
    have = {n for n, _, _ in env}
    for name, value in EXTRA_ENV:
        if name not in have:
            env.append((name, None, value))

    L = ["properties:", "  template:", "    containers:",
         "      - image: " + cfg["image"],
         "        name: " + cfg["name"],
         "        resources:",
         "          cpu: " + str(cfg["cpu"]),
         "          memory: " + str(cfg["memory"]),
         "        env:"]
    for name, secret_ref, value in env:
        L.append("          - name: " + name)
        if secret_ref:
            L.append("            secretRef: " + secret_ref)
        else:
            L.append("            value: " + json.dumps(value or ""))
    L += ["        volumeMounts:",
          "          - volumeName: " + SHARE,
          "            mountPath: " + MOUNT_PATH,
          "    volumes:",
          "      - name: " + SHARE,
          "        storageName: " + SHARE,
          "        storageType: AzureFile",
          "    scale:",
          "      minReplicas: %d" % (cfg["scale"].get("minReplicas") or 1),
          "      maxReplicas: %d" % (cfg["scale"].get("maxReplicas") or 10)]
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="make the changes")
    args = ap.parse_args()

    print("reading the app's current settings...")
    cfg = az_json(
        'containerapp show -n %s -g %s --query "{image:properties.template.containers[0].image, '
        'name:properties.template.containers[0].name, cpu:properties.template.containers[0].resources.cpu, '
        'memory:properties.template.containers[0].resources.memory, env:properties.template.containers[0].env, '
        'scale:properties.template.scale, secrets:properties.configuration.secrets[].name, '
        'ingress:properties.configuration.ingress.fqdn}" -o json' % (APP, RG))
    account = storage_account_name()
    yaml_text = build_yaml(cfg)

    print("\nplan")
    print("  storage account : %s  (Standard_LRS, East US 2)" % account)
    print("  file share      : %s  (1 GiB)" % SHARE)
    print("  mounted at      : %s  in %s" % (MOUNT_PATH, APP))
    print("  env added       : " + ", ".join("%s=%s" % kv for kv in EXTRA_ENV))
    print("  preserved       : image %s" % cfg["image"].split("/")[-1])
    print("                    secrets %s" % ", ".join(cfg.get("secrets") or []))
    print("                    %s cpu / %s memory, replicas %s-%s"
          % (cfg["cpu"], cfg["memory"], cfg["scale"].get("minReplicas"),
             cfg["scale"].get("maxReplicas")))

    with open(YAML_PATH, "w", encoding="utf-8") as fh:
        fh.write(yaml_text)
    print("\nYAML written to %s:\n" % YAML_PATH)
    print("".join("    " + line + "\n" for line in yaml_text.splitlines()))

    if not args.apply:
        print("dry run -- nothing changed. Re-run with --apply to do it.")
        return

    print("1/4 storage account...")
    if not az_json('storage account list -g %s --query "[?name==\'%s\']" -o json' % (RG, account)):
        az("storage account create --name %s --resource-group %s --location eastus2 "
           "--sku Standard_LRS --kind StorageV2 --only-show-errors -o none" % (account, RG))
    print("    %s" % account)

    print("2/4 file share...")
    az("storage share-rm create --resource-group %s --storage-account %s --name %s "
       "--quota 1 --only-show-errors -o none" % (RG, account, SHARE))

    print("3/4 linking the share to the environment...")
    key = az('storage account keys list --account-name %s --resource-group %s '
             '--query "[0].value" -o tsv' % (account, RG))
    az('containerapp env storage set --name %s --resource-group %s --storage-name %s '
       '--azure-file-account-name %s --azure-file-account-key "%s" --azure-file-share-name %s '
       '--access-mode ReadWrite --only-show-errors -o none' % (ENV, RG, SHARE, account, key, SHARE))

    print("4/4 mounting it in the app...")
    az("containerapp update -n %s -g %s --yaml %s --only-show-errors -o none" % (APP, RG, YAML_PATH))

    after = az_json('containerapp show -n %s -g %s --query "{mounts:properties.template.containers[0].volumeMounts, '
                    'secrets:properties.configuration.secrets[].name, fqdn:properties.configuration.ingress.fqdn}" '
                    '-o json' % (APP, RG))
    print("\nafter:")
    print("  mounts  : %s" % after.get("mounts"))
    print("  secrets : %s" % after.get("secrets"))
    print("  url     : https://%s" % after.get("fqdn"))
    missing = set(cfg.get("secrets") or []) - set(after.get("secrets") or [])
    if missing:
        print("\n!! these secrets did not survive the update: %s" % ", ".join(sorted(missing)))
        print("   re-run:  python set_azure_secrets.py")
    elif not after.get("mounts"):
        print("\n!! the mount is not showing up -- check the YAML above")
    else:
        print("\nDone. The scorecard now persists across deploys.")


if __name__ == "__main__":
    main()
