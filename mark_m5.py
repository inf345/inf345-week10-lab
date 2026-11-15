#!/usr/bin/env python3
"""
INF 345 Milestone 5 - GitOps: Argo CD deploys your service from Git.

Due Sunday 22 November 2026, 23:59 Almaty time (UTC+5) = 18:59 UTC. Deadlines are judged
by commit time.

    python3 mark_m5.py --path .                  # the static checks: no cluster needed
    python3 mark_m5.py --path . --kind           # + the live checks: kind + Argo CD, thrown away
    python3 mark_m5.py --repo https://github.com/<you>/<project> --kind --at-deadline

WHAT M5 ASKS FOR - the contract
  argocd/application.yaml   ONE Argo CD Application:
                              source.repoURL  your own public repository,
                                              https://github.com/<you>/<project>
                              source.path     the folder Argo CD deploys: k8s (your M4 files),
                                              or a Helm chart, or a Kustomize folder
                              syncPolicy.automated with prune: true and selfHeal: true
  that folder               renders to manifests that pass the M4 safety rules: allowed kinds,
                            your own image by its full commit SHA, no Secret, nothing unsafe
  README.md                 a "Rollback" section that says how you roll back: git revert
  .github/workflows/        CI still builds and pushes your image - and NOTHING in CI deploys
  your git history          at least one commit that changes the image tag: a deploy via Git

THE SECRET CONVENTION (from M4). No Secret in Git. The marker creates `app-secret` itself,
with a new random value for every key your Deployment reads, BEFORE Argo CD starts. It is not
in your path, so Argo CD never manages it - and the marker checks that it stayed that way.

HOW THE LIVE CHECKS WORK. The marker never applies your Application object as it is. It reads
the fields it needs (repoURL, path, Helm values, your sync policy), checks them, and creates its
OWN Application from them, pinned to the commit being marked (a full SHA, not a branch), in a
project that allows only your repository, one namespace (m5) and the M4 kinds. Argo CD 3.5.3
("core" install: no UI) runs in a throwaway kind cluster (Kubernetes 1.36.4 - the newest version
Argo CD 3.5 is tested on). The marker renders your path itself first - plain files, Helm 4.2.1
or Kustomize 5.8.1, the same tools Argo CD 3.5.3 uses - and checks the result BEFORE anything
reaches the cluster. After the sync it checks that Argo CD deployed exactly what it checked.

Gates. Any gate failure scores the milestone 0.
  application     argocd/ holds one Application; repoURL is your repository; path exists
  renders         your path renders, as Argo CD would render it
  manifests       only the allowed kinds; one Deployment; a Service; your own image
  safe to run     no hostPath, privileged, host namespaces, added capabilities... (= M4)
  no secrets      no Secret objects and no secret values - in what Argo CD would deploy, in
                  your values files, and in the Application
  no credentials  no credential anywhere in the git history (the same rule as M1-M4)
  as checked      (live) Argo CD deployed exactly the objects the marker checked

Then 100 points in four blocks of 25. A block scores 25 only if every check in it passes.
  it syncs        LIVE: the marker's Application (your fields, pinned to the marked commit)
                  is Synced and Healthy within 300 s; the Pods run the image named in Git;
                  Argo CD leaves app-secret alone
  it heals        syncPolicy.automated with prune: true and selfHeal: true; replicas set in Git;
                  LIVE: the Deployment is scaled by hand -> Argo CD puts it back; the Service is
                  deleted -> Argo CD recreates it; an object Argo CD owns but Git does not have
                  -> Argo CD deletes it (prune)
  build is        CI still builds and pushes your image; no workflow - and no script a workflow
  separated from  runs - deploys: no kubectl apply/patch/set image, no helm install/upgrade, no
  deploy          argocd app sync, no cluster credentials. A job that creates its own throwaway
                  kind cluster to TEST your files is not a deploy
  the version is  every image pinned by its full commit SHA (or a digest), never latest; your
  pinned and      README's Rollback section names git revert; your history shows an image bump:
  reversible      a commit that moved the image tag from one pinned version to another

A block the marker could not check (the cluster would not start, GitHub did not answer)
says NOT CHECKED. That is not a zero: it means "run it again".

Exit code: with a cluster, 0 only if the score is 100; without one, 0 if no static check
failed. Nothing in this file is hidden from students: it is the rubric.
"""

import argparse
import base64
import copy
import datetime
import hashlib
import json
import os
import random
import re
import secrets as pysecrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

try:
    import yaml
except ImportError:
    print("::error::PyYAML is required: sudo apt install python3-yaml  (or pip install pyyaml)")
    sys.exit(2)


DEADLINE = "2026-11-22T23:59:59+05:00"          # Sunday 22 Nov, 23:59 Almaty (UTC+5)
DEADLINE_UTC = (datetime.datetime.fromisoformat(DEADLINE)
                .astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
# Argo CD 3.5 is tested on Kubernetes 1.33-1.36 (docs/operator-manual/tested-kubernetes-
# versions.md), so the marker's cluster is 1.36.4 - kind v0.33.0's image, pinned by digest.
KIND_NODE_IMAGE = ("kindest/node:v1.36.4@sha256:"
                   "099e049362a1526b2db71494e1947aae99bd16290d7c895f2b7ea312e3cbfaed")
KUBE_VERSION = "v1.36.4"          # what Argo CD tells helm (--kube-version) on that cluster
KIND_CLUSTER = "inf345-m5"
ARGOCD_VERSION = "v3.5.3"
ARGOCD_MANIFEST_URL = ("https://raw.githubusercontent.com/argoproj/argo-cd/"
                       f"{ARGOCD_VERSION}/manifests/core-install.yaml")
ARGOCD_MANIFEST_SHA256 = "1a87025d8eb2eae621653fd312fb9ca51df1b4b3b6992a030e3a9ef38e45c448"
HELM_VERSION = "v4.2.1"           # the Helm that Argo CD v3.5.3 bundles (hack/tool-versions.sh)
NS_ARGO = "argocd"
NS_APP = "m5"                     # the only namespace the marker's project allows
APP_NAME = "m5"                   # the marker's Application
PROJECT = "m5"                    # the marker's AppProject
SECRET_NAME = "app-secret"
DEFAULT_SECRET_KEY = "API_KEY"    # created when app-secret is used without naming keys
RESCUE_IMAGES = ("ghcr.io/inf345/notes-rescue",)
APP_DIR = "argocd"
INSTALL_SECONDS = 300             # each Argo CD component must be ready within this
SYNC_SECONDS = 300                # Synced and Healthy within this, after the Application exists
HEAL_SECONDS = 120                # each repair must happen within this
SETTLE_SECONDS = 60               # back to Synced between the heal tests
POLL_SECONDS = 3
PRUNE_PROBE = "m5-prune-check"
SYSTEM_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease", "argocd",
                     "local-path-storage"}
TRACKING = "argocd.argoproj.io/tracking-id"

# The M4 kinds. Argo CD applies EVERY object it renders, so in M5 anything else is refused -
# there is no "read but not applied" any more. The groups are for the AppProject allowlist.
ALLOWED = {"Deployment": "apps", "Service": "", "ConfigMap": "", "ServiceAccount": "",
           "PodDisruptionBudget": "policy", "HorizontalPodAutoscaler": "autoscaling",
           "NetworkPolicy": "networking.k8s.io"}
ALLOWED_KINDS = set(ALLOWED)
SAFE_VOLUME_TYPES = {"configMap", "secret", "emptyDir", "projected", "downwardAPI"}
PROBES = ("livenessProbe", "readinessProbe", "startupProbe")
# Argo CD's Directory source reads these files (reposerver/repository/repository.go).
MANIFEST_FILE = re.compile(r"^.*\.(yaml|yml|json|jsonnet)$")
KUSTOMIZATION_NAMES = ("kustomization.yaml", "kustomization.yml", "Kustomization")
HELM_OPTIONS = {"valueFiles", "values", "valuesObject", "parameters", "releaseName",
                "ignoreMissingValueFiles", "version", "skipTests", "skipSchemaValidation"}
SYNC_OPTIONS_COPIED = re.compile(r"^(ServerSideApply|ApplyOutOfSyncOnly|PruneLast|"
                                 r"PrunePropagationPolicy|FailOnSharedResource|"
                                 r"SkipDryRunOnMissingResource)=\S+$")
PINNED = re.compile(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])|sha256:[0-9a-f]{64}")


# ================================================================ helpers (= M4)

def run(cmd, cwd=None, timeout=120, env=None, input=None):
    """Run a command, never raise. Returns (rc, stdout, stderr)."""
    e = dict(os.environ)
    if env:
        e.update(env)
    try:
        p = subprocess.run(cmd, cwd=cwd, env=e, timeout=timeout, capture_output=True,
                           encoding="utf-8", errors="replace", input=input)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except OSError as exc:
        return 127, "", str(exc)


def read_text(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def repo_slug(url):
    m = re.search(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", url or "")
    return f"{m.group(1)}/{m.group(2)}" if m else None


def dig(d, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
        if d is None:
            return default
    return d


def as_int(v, default=None):
    if isinstance(v, bool):
        return default
    if isinstance(v, int):
        return v
    if isinstance(v, str) and re.fullmatch(r"\s*\d+\s*", v):
        return int(v)
    return default


def short(text, n=240):
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text if len(text) <= n else text[:n - 3] + "..."


class Clock:
    """Time, replaceable in the tests."""
    def now(self):
        return time.monotonic()

    def sleep(self, s):
        time.sleep(s)


# ======================================================= credentials (= M1-M4)

HIGH_CONFIDENCE = [
    (r"AKIA[0-9A-Z]{16}", "AWS access key id"),
    (r"gh[pousr]_[A-Za-z0-9]{36,}", "GitHub token"),
    (r"github_pat_[A-Za-z0-9_]{50,}", "GitHub fine-grained token"),
    (r"-----BEGIN (RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----", "private key"),
    (r"xox[baprs]-[A-Za-z0-9-]{10,}", "Slack token"),
]
DOC_VALUES = re.compile(r"(?i)(EXAMPLE|wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY)")
FUZZY = [
    (r"(?i)\b(api[_-]?key|secret|password|passwd|token)\s*[:=]\s*"
     r"['\"][^'\"\s]{12,}['\"]", "hardcoded credential"),
]
PLACEHOLDER = re.compile(
    r"(?i)(example|sample|placeholder|dummy|change[_-]?me|replace[_-]?me"
    r"|your[_\-\w]*|[_\-]here\b|xxx+|<[^>]+>|\$\{[^}]+\}|\$\([^)]+\)|\bfake\b"
    r"|redacted|todo|test[_-]?token|secret[_-]?key[_-]?here)")
SECRETISH_NAME = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|private[_-]?key"
                            r"|credential|access[_-]?key)")


def scan_line(line):
    for pat, label in HIGH_CONFIDENCE:
        m = re.search(pat, line)
        if m and not DOC_VALUES.search(m.group(0)):
            return f"{label}: {m.group(0)[:40]}"
    for pat, label in FUZZY:
        m = re.search(pat, line)
        if m and not PLACEHOLDER.search(m.group(0)):
            return f"{label}: {m.group(0)[:60]}"
    return None


# ============================================================ objects (= M4)

def of_kind(docs, kind):
    return [d for d in docs if d.get("kind") == kind]


def name_of(doc):
    return dig(doc, "metadata", "name", default="") or ""


def pod_spec(dep):
    return dig(dep, "spec", "template", "spec", default={}) or {}


def all_containers(spec):
    out = []
    for field in ("initContainers", "containers", "ephemeralContainers"):
        out += [c for c in (spec.get(field) or []) if isinstance(c, dict)]
    return out


def split_image(ref):
    """'ghcr.io/o/r:tag@sha256:d' -> (repo, tag or None, digest or None)."""
    ref = (ref or "").strip()
    digest = None
    if "@" in ref:
        ref, digest = ref.split("@", 1)
    last = ref.rsplit("/", 1)[-1]
    tag = None
    if ":" in last:
        ref, tag = ref.rsplit(":", 1)
    return ref, tag, digest


def is_rescue(repo):
    return any(repo.lower() == r for r in RESCUE_IMAGES)


def app_container(dep, expected_repo):
    cs = [c for c in (pod_spec(dep).get("containers") or []) if isinstance(c, dict)]
    if not cs:
        return None
    for c in cs:
        repo = split_image(c.get("image"))[0].lower()
        if repo == (expected_repo or "").lower() or is_rescue(repo):
            return c
    return cs[0]


def label_selector(dep):
    ml = dig(dep, "spec", "selector", "matchLabels", default={}) or {}
    return ",".join(f"{k}={v}" for k, v in sorted(ml.items())) if isinstance(ml, dict) else ""


def config_map_refs(container, spec):
    names = set()
    for e in container.get("env") or []:
        ref = dig(e, "valueFrom", "configMapKeyRef")
        if ref and not ref.get("optional"):
            names.add(ref.get("name"))
    for ef in container.get("envFrom") or []:
        if isinstance(ef, dict) and ef.get("configMapRef") and not ef["configMapRef"].get("optional"):
            names.add(ef["configMapRef"].get("name"))
    for v in spec.get("volumes") or []:
        if isinstance(v, dict) and v.get("configMap") and not v["configMap"].get("optional"):
            names.add(v["configMap"].get("name"))
    return {n for n in names if n}


def secret_refs(container, spec):
    """-> list of (secret name, key or None, optional, how) the app container uses."""
    refs = []
    for e in container.get("env") or []:
        ref = dig(e, "valueFrom", "secretKeyRef")
        if ref:
            refs.append((ref.get("name"), ref.get("key"), bool(ref.get("optional")),
                         f"env {e.get('name')}"))
    for ef in container.get("envFrom") or []:
        if isinstance(ef, dict) and ef.get("secretRef"):
            refs.append((ef["secretRef"].get("name"), None, bool(ef["secretRef"].get("optional")),
                         "envFrom"))
    mounted = {m.get("name") for m in (container.get("volumeMounts") or []) if isinstance(m, dict)}
    for v in spec.get("volumes") or []:
        if not isinstance(v, dict) or v.get("name") not in mounted:
            continue
        sources = []
        if v.get("secret"):
            sources.append((v["secret"].get("secretName"), v["secret"]))
        for src in dig(v, "projected", "sources", default=[]) or []:
            if isinstance(src, dict) and src.get("secret"):
                sources.append((src["secret"].get("name"), src["secret"]))
        for name, s in sources:
            items = [i.get("key") for i in (s.get("items") or []) if isinstance(i, dict)]
            if items:
                refs += [(name, k, bool(s.get("optional")), f"file {k}") for k in items]
            else:
                refs.append((name, None, bool(s.get("optional")), "mounted volume"))
    return refs


def secret_object(keys):
    return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": SECRET_NAME},
            "stringData": {k: pysecrets.token_hex(16) for k in sorted(keys)}}


def secret_keys(container, spec):
    keys = {k for n, k, _, _ in secret_refs(container, spec) if n == SECRET_NAME and k}
    if any(n == SECRET_NAME and not k for n, k, _, _ in secret_refs(container, spec)):
        keys.add(DEFAULT_SECRET_KEY)
    return keys or {DEFAULT_SECRET_KEY}


def container_status(pod, name):
    for cs in dig(pod, "status", "containerStatuses", default=[]) or []:
        if cs.get("name") == name:
            return cs
    return {}


def pod_ready(pod):
    return any(c.get("type") == "Ready" and c.get("status") == "True"
               for c in dig(pod, "status", "conditions", default=[]) or [])


def group_of(doc):
    api = str(doc.get("apiVersion") or "")
    return api.split("/", 1)[0] if "/" in api else ""


# ============================================================ the context

class Ctx(dict):
    """Everything the gates and blocks read. A dict, so the report is easy to build."""


# ============================================================ the Application

def github_slug(url):
    """'https://github.com/O/R(.git)(/)' -> 'O/R', anything else -> None."""
    m = re.fullmatch(r"https://github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?",
                     (url or "").strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def load_application(root):
    """Read the YAML files directly in argocd/. -> (app, file, problems, notes, raws)."""
    adir = os.path.join(root, APP_DIR)
    if not os.path.isdir(adir):
        return None, None, ["there is no argocd/ folder at the top of your repository - put your "
                            "Application in argocd/application.yaml"], [], {}
    apps, problems, notes, raws = [], [], [], {}
    files = sorted(f for f in os.listdir(adir)
                   if f.endswith((".yaml", ".yml")) and os.path.isfile(os.path.join(adir, f)))
    for f in files:
        rel = f"{APP_DIR}/{f}"
        text = read_text(os.path.join(adir, f))
        raws[rel] = text
        try:
            loaded = list(yaml.safe_load_all(text))
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            where = f" (line {mark.line + 1})" if mark else ""
            problems.append(f"{rel}{where} is not valid YAML: "
                            f"{short(str(getattr(exc, 'problem', exc)), 90)}")
            continue
        for doc in loaded:
            if not isinstance(doc, dict):
                continue
            if doc.get("kind") == "Application" and str(doc.get("apiVersion", "")).startswith("argoproj.io/"):
                apps.append((doc, rel))
            elif doc.get("kind"):
                notes.append(f"{rel}: {doc.get('kind')} {name_of(doc)} is ignored - the marker "
                             f"creates its own project")
    if not apps and not problems:
        problems.append("argocd/ has no Argo CD Application (apiVersion: argoproj.io/v1alpha1, "
                        "kind: Application)")
    if len(apps) > 1:
        problems.append(f"argocd/ has {len(apps)} Applications ("
                        f"{', '.join(r for _, r in apps)}) - M5 marks exactly one")
    app, file = apps[0] if len(apps) == 1 else (None, None)
    return app, file, problems, notes, raws


def clean_path(p):
    """The Application's path as a clean relative POSIX path, or None if it is unsafe."""
    if not isinstance(p, str) or not p.strip():
        return None
    p = p.strip().replace("\\", "/")
    if p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        return None
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


def gate_application(ctx):
    if ctx["app"] is None:
        return False, "; ".join(ctx["app_problems"]) or "no Application"
    app, problems = ctx["app"], []
    spec = app.get("spec") or {}
    if spec.get("sources"):
        problems.append("spec.sources (several sources) is not supported in M5 - use one "
                        "spec.source")
    src = spec.get("source")
    if not isinstance(src, dict):
        problems.append("the Application has no spec.source")
        return False, "; ".join(problems)
    url = str(src.get("repoURL") or "")
    slug = github_slug(url)
    if not url:
        problems.append("spec.source.repoURL is missing")
    elif not slug:
        if url.startswith(("git@", "ssh://")):
            problems.append(f"repoURL {url!r} is an SSH address - Argo CD would need a key. Your "
                            f"repository is public: use https://github.com/<you>/<project>")
        else:
            problems.append(f"repoURL {url!r} is not https://github.com/<you>/<project>")
    elif not ctx["slug"]:
        problems.append("cannot work out your repository's name - run the marker with --repo, "
                        "or from a clone whose 'origin' is on GitHub")
    elif slug.lower() != ctx["slug"].lower():
        problems.append(f"repoURL points at {slug}, not at your repository {ctx['slug']} - Argo CD "
                        f"would deploy someone else's files")
    for k, why in (("chart", "a chart from a Helm repository - M5 deploys your own repository: use "
                             "path"),
                   ("plugin", "a config management plugin - not supported in M5"),
                   ("ref", "a multi-source reference - not supported in M5")):
        if src.get(k):
            problems.append(f"spec.source.{k} is {why}")
    if sum(1 for k in ("helm", "kustomize", "directory") if isinstance(src.get(k), dict)) > 1:
        problems.append("spec.source sets more than one of helm, kustomize and directory - Argo CD "
                        "refuses it: \"multiple application sources defined\"")
    path = clean_path(src.get("path"))
    if src.get("path") in (None, ""):
        problems.append("spec.source.path is missing - name the folder Argo CD deploys, e.g. k8s")
    elif path is None:
        problems.append(f"spec.source.path {src.get('path')!r} must be a folder inside your "
                        f"repository (relative, no '..'), e.g. k8s - not the repository root")
    elif path.split("/")[0] in (APP_DIR, ".git", ".github"):
        problems.append(f"spec.source.path {path!r} is not where your manifests are - point it at "
                        f"the folder with your Deployment, e.g. k8s")
    else:
        full = os.path.join(ctx["root"], path)
        real_root = os.path.realpath(ctx["root"])
        if not os.path.exists(full):
            problems.append(f"spec.source.path '{path}' does not exist in your repository - Argo CD "
                            f"would show ComparisonError \"{path}: app path does not exist\"")
        elif not os.path.isdir(full):
            problems.append(f"spec.source.path '{path}' is a file - Argo CD wants a folder "
                            f"(\"app path is not a directory\")")
        elif not (os.path.realpath(full) + os.sep).startswith(real_root + os.sep):
            problems.append(f"spec.source.path '{path}' leads outside your repository")
    if isinstance(src.get("directory"), dict):
        extra = sorted(set(src["directory"]) - {"recurse"})
        if extra:
            problems.append(f"directory.{extra[0]} is not supported by the marker - remove it "
                            f"(only directory.recurse is)")
    if isinstance(src.get("helm"), dict):
        extra = sorted(set(src["helm"]) - HELM_OPTIONS)
        if extra:
            problems.append(f"helm.{extra[0]} is not supported by the marker - use valueFiles, "
                            f"values or parameters")
        for vf in src["helm"].get("valueFiles") or []:
            if not isinstance(vf, str) or re.match(r"^[a-z]+://", vf) or vf.startswith("$"):
                problems.append(f"helm.valueFiles entry {vf!r} must be a file in your repository")
    if isinstance(src.get("kustomize"), dict) and src["kustomize"]:
        problems.append(f"spec.source.kustomize.{sorted(src['kustomize'])[0]} is not supported - "
                        f"put it in kustomization.yaml, where the marker reads it")
    if problems:
        return False, "; ".join(dict.fromkeys(problems))
    ctx["source"], ctx["source_path"] = src, path
    auto = dig(spec, "syncPolicy", "automated")
    sync = ("automated" + (" (prune, selfHeal)" if isinstance(auto, dict) and auto.get("prune") is True
                           and auto.get("selfHeal") is True else "")) if isinstance(auto, dict) else "manual sync"
    return True, f"{ctx['app_file']}: path {path} in {url}, {sync}"


# ================================================================ rendering

def source_type(src, folder):
    """As Argo CD decides (pkg/apis/.../types.go ExplicitType, util/app/discovery)."""
    for k, t in (("kustomize", "Kustomize"), ("helm", "Helm"), ("directory", "Directory")):
        if isinstance(src.get(k), dict):
            return t
    names = os.listdir(folder)
    if any(n in KUSTOMIZATION_NAMES for n in names):
        return "Kustomize"
    if any(n.endswith("Chart.yaml") for n in names):
        return "Helm"
    return "Directory"


def text_files(root, base):
    """{repo-relative path: text} for the readable files under base (for the secret scan)."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        for fn in sorted(filenames):
            p = os.path.join(dirpath, fn)
            if os.path.islink(p) or os.path.getsize(p) > 1_000_000:
                continue
            try:
                with open(p, encoding="utf-8") as f:
                    out[os.path.relpath(p, root)] = f.read()
            except (UnicodeDecodeError, OSError):
                continue
    return out


def parse_objects(text, label, problems, notes, lenient=False):
    """Kubernetes objects from a YAML/JSON stream. lenient = Argo CD's rule for a Directory
    source: a file that does not look like a manifest is skipped, not an error."""
    objs = []
    try:
        loaded = list(yaml.safe_load_all(text))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark else ""
        if lenient and not all(w in text for w in ("apiVersion:", "kind:", "metadata:")):
            notes.append(f"{label} is skipped: not a Kubernetes manifest (Argo CD skips it too)")
            return []
        problems.append(f"{label}{where} is not valid YAML: "
                        f"{short(str(getattr(exc, 'problem', exc)), 90)}")
        return []
    for i, doc in enumerate(loaded):
        if doc is None:
            continue
        items = (doc.get("items") or []) if isinstance(doc, dict) and str(doc.get("kind", "")).endswith("List") \
            and isinstance(doc.get("items"), list) else [doc]
        for item in items:
            if not isinstance(item, dict) or not item.get("kind") or not item.get("apiVersion"):
                if lenient and not objs and not all(w in text for w in ("apiVersion:", "kind:", "metadata:")):
                    notes.append(f"{label} is skipped: not a Kubernetes manifest (Argo CD skips it "
                                 f"too)")
                    return []
                problems.append(f"{label}: document {i + 1} is not a Kubernetes object "
                                f"(no kind or apiVersion)")
                continue
            if item.get("kind") in ALLOWED_KINDS and not name_of(item):
                problems.append(f"{label}: {item.get('kind')} has no metadata.name")
                continue
            item["_file"] = label
            objs.append(item)
    return objs


def render_directory(ctx, folder, recurse):
    docs, problems, notes = [], [], []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted(dirnames) if recurse else []
        for fn in sorted(filenames):
            if not MANIFEST_FILE.match(fn):
                continue
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, ctx["root"])
            if not (os.path.realpath(p)).startswith(os.path.realpath(ctx["root"]) + os.sep):
                problems.append(f"{rel} is a link to a file outside your repository")
                continue
            if fn.endswith(".jsonnet"):
                problems.append(f"{rel}: Argo CD would run this jsonnet program - the marker does "
                                f"not; keep plain YAML in the folder")
                continue
            text = read_text(p)
            if fn.endswith(".json"):
                try:
                    obj = json.loads(text)
                except ValueError as exc:
                    problems.append(f"{rel} is not valid JSON: {short(str(exc), 90)}")
                    continue
                docs += parse_objects(json.dumps(obj), rel, problems, notes)
            else:
                docs += parse_objects(text, rel, problems, notes, lenient=True)
    if not recurse:
        nested = sorted(os.path.relpath(os.path.join(d, f), ctx["root"])
                        for d, _, fs in os.walk(folder) if d != folder
                        for f in fs if MANIFEST_FILE.match(f))
        if nested and not docs:
            notes.append(f"{nested[0]} is in a subfolder: Argo CD reads only the top of the path "
                         f"(unless directory.recurse: true)")
    return docs, problems, notes


def render_helm(ctx, folder, helm):
    docs, problems, notes = [], [], []
    if not shutil.which("helm"):
        return None, [f"{TOOL}your path is a Helm chart, and helm is not installed here - install "
                      f"Helm {HELM_VERSION[1:]} (the lab page), or let your pull-request job run "
                      f"the marker"], notes
    rc, out, _ = run(["helm", "version", "--short"], timeout=30)
    if rc == 0 and not out.strip().startswith(HELM_VERSION):
        notes.append(f"helm {out.strip()} rendered your chart; Argo CD {ARGOCD_VERSION} uses "
                     f"{HELM_VERSION} - the official run uses {HELM_VERSION} too")
    chart_file = os.path.join(folder, "Chart.yaml")
    try:
        chart = yaml.safe_load(read_text(chart_file)) if os.path.isfile(chart_file) else {}
    except yaml.YAMLError:
        chart = {}
    if isinstance(chart, dict) and chart.get("dependencies"):
        problems.append("your chart has dependencies - not supported in M5 (the marker and Argo "
                        "CD would have to download them); deploy only your own templates")
        return None, problems, notes
    release = str((helm or {}).get("releaseName") or name_of(ctx["app"]) or "notes")
    ctx["release"] = release
    args = ["helm", "template", folder, "--name-template", release, "--namespace", NS_APP,
            "--kube-version", KUBE_VERSION]
    for prm in (helm or {}).get("parameters") or []:
        if not isinstance(prm, dict) or not prm.get("name"):
            problems.append("a helm parameter has no name")
            continue
        flag = "--set-string" if prm.get("forceString") else "--set"
        args += [flag, f"{prm['name']}={prm.get('value', '')}"]
    real_root = os.path.realpath(ctx["root"])
    for vf in (helm or {}).get("valueFiles") or []:
        p = os.path.normpath(os.path.join(folder, str(vf)))
        rel = os.path.relpath(p, ctx["root"])
        if not (os.path.realpath(p) + os.sep).startswith(real_root + os.sep):
            problems.append(f"value file {vf} leads outside your repository")
        elif not os.path.isfile(p):
            if (helm or {}).get("ignoreMissingValueFiles"):
                notes.append(f"value file {rel} does not exist - ignored (ignoreMissingValueFiles)")
            else:
                problems.append(f"value file {rel} (helm.valueFiles) does not exist - Argo CD "
                                f"would show ComparisonError")
        else:
            args += ["--values", p]
            ctx["value_files"].append(rel)
    tmp = None
    inline = (helm or {}).get("valuesObject")
    if inline is None and (helm or {}).get("values"):
        try:
            inline = yaml.safe_load((helm or {}).get("values"))
        except yaml.YAMLError as exc:
            problems.append(f"helm.values is not valid YAML: {short(str(exc), 90)}")
    if isinstance(inline, dict) and inline:
        fd, tmp = tempfile.mkstemp(prefix="m5-values-", suffix=".yaml")
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(inline, f)
        args += ["--values", tmp]
    args.append("--include-crds")
    if (helm or {}).get("skipTests"):
        args.append("--skip-tests")
    if (helm or {}).get("skipSchemaValidation"):
        args.append("--skip-schema-validation")
    if problems:
        return None, problems, notes
    try:
        rc, out, err = run(args, timeout=120)
    finally:
        if tmp:
            os.remove(tmp)
    if rc != 0:
        return None, [f"helm template failed - Argo CD would show ComparisonError: "
                      f"{short(err or out, 400)}"], notes
    ctx["rendered_text"] = out
    current = "rendered by helm"
    for chunk in re.split(r"(?m)^---\s*$", out):
        m = re.search(r"(?m)^# Source: (.+)$", chunk)
        label = f"{ctx['source_path']} (helm: {m.group(1).strip()})" if m else f"{ctx['source_path']} ({current})"
        if chunk.strip():
            docs += parse_objects(chunk, label, problems, notes)
    return docs, problems, notes


TOOL = "TOOL MISSING: "          # a render problem caused by this machine, not by the files
REMOTE_REF = re.compile(r"(://|^github\.com/|^git@|^gitlab\.com/|^bitbucket\.org/)")


def render_kustomize(ctx, folder):
    """kubectl kustomize - kubectl 1.37.1 embeds Kustomize 5.8.1, the version Argo CD 3.5.3
    bundles. First every kustomization it would read is checked: nothing from the internet."""
    problems, notes = [], []
    real_root = os.path.realpath(ctx["root"])
    queue, seen = [folder], set()
    while queue:
        d = os.path.realpath(queue.pop(0))
        if d in seen:
            continue
        seen.add(d)
        kf = next((os.path.join(d, n) for n in KUSTOMIZATION_NAMES
                   if os.path.isfile(os.path.join(d, n))), None)
        if not kf:
            continue
        rel = os.path.relpath(kf, real_root)
        try:
            k = yaml.safe_load(read_text(kf)) or {}
        except yaml.YAMLError as exc:
            problems.append(f"{rel} is not valid YAML: {short(str(exc), 90)}")
            continue
        if not isinstance(k, dict):
            continue
        refs = []
        for field in ("resources", "components", "bases"):
            refs += [r for r in (k.get(field) or []) if isinstance(r, str)]
        refs += [p.get("path") for p in (k.get("patches") or []) if isinstance(p, dict)
                 and isinstance(p.get("path"), str)]
        for r in refs:
            if REMOTE_REF.search(r.strip()):
                problems.append(f"{rel} pulls {r!r} from the internet - M5 deploys only what is "
                                f"in your repository")
                continue
            target = os.path.realpath(os.path.join(d, r))
            if not (target + os.sep).startswith(real_root + os.sep):
                problems.append(f"{rel} reads {r!r}, which is outside your repository")
            elif os.path.isdir(target):
                queue.append(target)
        if k.get("helmCharts"):
            problems.append(f"{rel} uses helmCharts - not supported in M5")
    # the bases an overlay reads count as "the path" for the image-bump search
    ctx["extra_paths"] = sorted(os.path.relpath(d, real_root) for d in seen
                                if d != os.path.realpath(folder))
    if problems:
        return None, problems, notes
    if not shutil.which("kubectl"):
        return None, [f"{TOOL}your path is a Kustomize folder, and kubectl is not installed here - "
                      f"kubectl kustomize renders it (Week 8's install)"], notes
    rc, out, err = run(["kubectl", "kustomize", folder], timeout=120)
    if rc != 0:
        return None, [f"kustomize build failed - Argo CD would show ComparisonError: "
                      f"{short(err or out, 400)}"], notes
    ctx["rendered_text"] = out
    docs = parse_objects(out, f"{ctx['source_path']} (kustomize)", problems, notes)
    return docs, problems, notes


def render(ctx):
    """Render the Application's path as Argo CD would. Sets ctx docs/raws/source_type."""
    src, path = ctx["source"], ctx["source_path"]
    folder = os.path.join(ctx["root"], path)
    problems = []
    overrides = [f for f in os.listdir(folder) if re.fullmatch(r"\.argocd-source(-.+)?\.ya?ml", f)]
    if overrides:
        problems.append(f"{path}/{overrides[0]} overrides the Application's parameters - the "
                        f"marker does not read it; remove it")
    stype = source_type(src, folder)
    ctx["source_type"] = stype
    if stype == "Helm":
        docs, more, notes = render_helm(ctx, folder, src.get("helm"))
    elif stype == "Kustomize":
        docs, more, notes = render_kustomize(ctx, folder)
    else:
        docs, more, notes = render_directory(ctx, folder, bool(dig(src, "directory", "recurse")))
    problems += more
    ctx["notes"].extend(notes)
    raws = dict(ctx["app_raws"])
    raws.update(text_files(ctx["root"], folder))
    for rel in ctx["value_files"]:
        p = os.path.join(ctx["root"], rel)
        if os.path.isfile(p):
            raws[rel] = read_text(p)
    if ctx.get("rendered_text"):
        raws[f"{path} ({stype.lower()} output)"] = ctx["rendered_text"]
    ctx["raws"] = raws
    if docs is not None and not docs and not problems:
        problems.append(f"{path} renders to no Kubernetes objects")
    ctx["docs"] = docs if docs is not None else None
    ctx["render_problems"] = problems
    if ctx["docs"]:
        deps = of_kind(ctx["docs"], "Deployment")
        if len(deps) == 1:
            ctx["dep"] = deps[0]
            ctx["container"] = app_container(deps[0], ctx["expected_repo"])


def gate_renders(ctx):
    probs = ctx["render_problems"]
    if probs and all(p.startswith(TOOL) for p in probs):
        return None, "; ".join(p[len(TOOL):] for p in probs)     # this machine, not your files
    if ctx["docs"] is None or probs:
        return False, "; ".join(p.replace(TOOL, "") for p in probs) or "nothing rendered"
    kinds = sorted({d.get("kind") for d in ctx["docs"]})
    what = "plain YAML files" if ctx["source_type"] == "Directory" else f"{ctx['source_type']} source"
    return True, f"{what}: {len(ctx['docs'])} object(s) - {', '.join(kinds)}"


# ================================================================= gates

def gate_manifests(ctx):
    docs, problems = ctx["docs"], []
    for d in docs:
        k = d.get("kind")
        if k == "Secret":
            continue                                   # the 'no secrets' gate explains it
        if k not in ALLOWED_KINDS:
            problems.append(f"{d['_file']}: kind {k} is not allowed in M5 - Argo CD applies every "
                            f"object in your path, and the marker's project allows only "
                            f"{', '.join(sorted(ALLOWED_KINDS))}. Move it out of the path")
    deps = of_kind(docs, "Deployment")
    if len(deps) != 1:
        problems.append(f"your path must hold exactly one Deployment - your service - and it "
                        f"has {len(deps)}")
    if not of_kind(docs, "Service"):
        problems.append("your path has no Service")
    expected = ctx["expected_repo"]
    if not expected:
        problems.append("cannot work out your repository's name - run the marker with --repo, or "
                        "from a clone whose 'origin' is on GitHub")
    for dep in deps:
        spec = pod_spec(dep)
        if not (spec.get("containers") or []):
            problems.append("the Deployment has no containers")
        for c in all_containers(spec):
            repo = split_image(c.get("image"))[0]
            if expected and repo != expected and not is_rescue(repo.lower()):
                hint = (" - image names must be lowercase"
                        if repo.lower() in (expected, *RESCUE_IMAGES) else "")
                problems.append(f"container '{c.get('name')}' runs {c.get('image')!r}: every "
                                f"container must run your own image, {expected}, or the course's "
                                f"rescue image{hint}")
    if problems:
        return False, "; ".join(dict.fromkeys(problems))
    return True, f"{len(docs)} object(s): {', '.join(sorted({d.get('kind') for d in docs}))}"


def gate_safe(ctx):
    """Refuse anything that could reach the machine the marker runs on."""
    problems = []
    for d in ctx["docs"] or []:
        where = f"{d['_file']} ({d.get('kind')} {name_of(d)})"
        if d.get("kind") == "Service":
            if dig(d, "spec", "externalIPs"):
                problems.append(f"{where}: spec.externalIPs is not allowed")
            continue
        if d.get("kind") != "Deployment":
            continue
        spec = pod_spec(d)
        for f in ("hostNetwork", "hostPID", "hostIPC"):
            if spec.get(f) is True:
                problems.append(f"{where}: {f}: true shares the machine's "
                                f"{f[4:].lower()} namespace - not allowed")
        if dig(spec, "securityContext", "sysctls"):
            problems.append(f"{where}: securityContext.sysctls is not allowed")
        for sc in (spec.get("securityContext") or {},):
            if dig(sc, "seccompProfile", "type") == "Unconfined":
                problems.append(f"{where}: seccompProfile Unconfined is not allowed")
            if sc.get("seLinuxOptions"):
                problems.append(f"{where}: seLinuxOptions are not allowed")
            if dig(sc, "appArmorProfile", "type") == "Unconfined":
                problems.append(f"{where}: appArmorProfile Unconfined is not allowed")
            if dig(sc, "windowsOptions", "hostProcess"):
                problems.append(f"{where}: hostProcess is not allowed")
        if spec.get("ephemeralContainers"):
            problems.append(f"{where}: ephemeralContainers cannot be declared in a template")
        for v in spec.get("volumes") or []:
            if not isinstance(v, dict):
                continue
            types = [k for k in v if k != "name"]
            bad = [t for t in types if t not in SAFE_VOLUME_TYPES]
            if bad:
                problems.append(f"{where}: volume '{v.get('name')}' is {bad[0]} - only "
                                f"configMap, secret, emptyDir, projected and downwardAPI "
                                f"volumes are allowed")
        for c in all_containers(spec):
            cn = f"{where} container '{c.get('name')}'"
            sc = c.get("securityContext") or {}
            if sc.get("privileged") is True:
                problems.append(f"{cn}: privileged: true is not allowed")
            if dig(sc, "capabilities", "add"):
                problems.append(f"{cn}: capabilities.add {dig(sc, 'capabilities', 'add')} is not "
                                f"allowed - drop capabilities, never add them")
            if sc.get("procMount") not in (None, "Default"):
                problems.append(f"{cn}: procMount {sc.get('procMount')} is not allowed")
            if dig(sc, "seccompProfile", "type") == "Unconfined":
                problems.append(f"{cn}: seccompProfile Unconfined is not allowed")
            if dig(sc, "appArmorProfile", "type") == "Unconfined":
                problems.append(f"{cn}: appArmorProfile Unconfined is not allowed")
            if sc.get("seLinuxOptions"):
                problems.append(f"{cn}: seLinuxOptions are not allowed")
            for p in c.get("ports") or []:
                if isinstance(p, dict) and p.get("hostPort"):
                    problems.append(f"{cn}: hostPort {p.get('hostPort')} is not allowed")
            for pr in PROBES:
                probe = c.get(pr) or {}
                if dig(probe, "httpGet", "host") or dig(probe, "tcpSocket", "host"):
                    problems.append(f"{cn}: {pr} sets a host - a probe checks your own Pod only")
            for hook in ("postStart", "preStop"):
                h = dig(c, "lifecycle", hook, default={}) or {}
                if dig(h, "httpGet", "host") or dig(h, "tcpSocket", "host"):
                    problems.append(f"{cn}: lifecycle.{hook} sets a host")
    if problems:
        seen, out = set(), []
        for p in problems:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return False, "; ".join(out)
    return True, "no host access, no privileged containers, no added capabilities"


def gate_no_secrets(ctx):
    problems = []
    for d in ctx["docs"] or []:
        if d.get("kind") == "Secret":
            problems.append(f"{d['_file']}: a Secret in Git is a leaked secret - base64 is not "
                            f"encryption. Delete this file; create '{SECRET_NAME}' on the cluster "
                            f"with kubectl (the marker creates it itself)")
        if d.get("kind") == "ConfigMap":
            for k, v in (d.get("data") or {}).items():
                if SECRETISH_NAME.search(str(k)) and isinstance(v, str) and len(v.strip()) >= 8 \
                        and not PLACEHOLDER.search(v):
                    problems.append(f"{d['_file']}: ConfigMap key '{k}' looks like a secret in "
                                    f"plain text - secrets belong in '{SECRET_NAME}'")
        if d.get("kind") == "Deployment":
            for c in all_containers(pod_spec(d)):
                for e in c.get("env") or []:
                    if not isinstance(e, dict) or "value" not in e:
                        continue
                    val = str(e.get("value") or "")
                    if SECRETISH_NAME.search(str(e.get("name", ""))) and len(val) >= 8 \
                            and not PLACEHOLDER.search(val):
                        problems.append(f"{d['_file']}: env {e.get('name')} has its value written "
                                        f"in the Deployment - take it from '{SECRET_NAME}' with "
                                        f"secretKeyRef")
    for rel, text in (ctx.get("raws") or {}).items():
        for line in text.splitlines():
            hit = scan_line(line)
            if hit:
                problems.append(f"{rel}: {hit}")
                break
    if problems:
        return False, "; ".join(dict.fromkeys(problems))
    return True, "no Secret objects and no secret values in what Argo CD would deploy"


def gate_no_credentials(ctx):
    root = ctx["repo_root"]
    if not root or not os.path.isdir(os.path.join(root, ".git")):
        return False, "not a git repository, so the history cannot be checked"
    rc, out, _ = run(["git", "log", "-p", "--all", "--no-color"], cwd=root, timeout=180)
    if rc != 0:
        return False, "could not read the git history"
    hits = [h for h in (scan_line(l) for l in out.splitlines() if l.startswith("+")) if h]
    if hits:
        return False, "credential found in git history - " + "; ".join(sorted(set(hits))[:3])
    return True, "no credentials anywhere in the git history"


STATIC_GATES = [("application", gate_application), ("renders", gate_renders),
                ("manifests", gate_manifests), ("safe to run", gate_safe),
                ("no secrets", gate_no_secrets), ("no credentials", gate_no_credentials)]


# ======================================================= static checks per block

def git_refs(repo_root):
    rc, out, _ = run(["git", "for-each-ref", "--format=%(refname)"], cwd=repo_root)
    names = set()
    for r in out.split() if rc == 0 else []:
        names.add(r)
        for pre in ("refs/heads/", "refs/remotes/origin/", "refs/tags/"):
            if r.startswith(pre):
                names.add(r[len(pre):])
    return names


def is_commit(repo_root, sha):
    if not repo_root or not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
        return False
    rc, _, _ = run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo_root)
    return rc == 0


def target_namespace(ctx):
    """Where Argo CD will put the objects: the one namespace they name, or NS_APP.
    -> (namespace, problem or None)."""
    named = {}
    for d in ctx["docs"]:
        ns = dig(d, "metadata", "namespace")
        if ns:
            named.setdefault(str(ns), d["_file"])
    if not named:
        return NS_APP, None
    if len(named) > 1:
        (a, fa), (b, fb) = sorted(named.items())[:2]
        return NS_APP, (f"your objects name two namespaces ({a} in {fa}, {b} in {fb}) - remove "
                        f"metadata.namespace and let the Application's destination decide")
    ns, f = next(iter(named.items()))
    if ns in SYSTEM_NAMESPACES or ns.startswith("kube-") or \
            not re.fullmatch(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?", ns):
        return NS_APP, (f"{f}: metadata.namespace {ns!r} - never deploy your app into a system "
                        f"namespace; remove the line")
    return ns, None


def static_syncs(ctx):
    problems, src, dep, c = [], ctx["source"], ctx["dep"], ctx["container"]
    rev = str(src.get("targetRevision") or "HEAD").strip()
    if rev != "HEAD" and not is_commit(ctx["repo_root"], rev) and rev not in git_refs(ctx["repo_root"]):
        problems.append(f"targetRevision {rev!r} is not a branch, tag or commit in your repository "
                        f"- your own Argo CD would show ComparisonError. Use HEAD or main")
    elif rev not in ("HEAD", "main", "master"):
        ctx["notes"].append(f"your Application follows {rev!r}; the marker deploys the commit it "
                            f"marks ({(ctx['sha'] or '')[:7]}) on your default branch")
    ns, why = target_namespace(ctx)
    ctx["ns"] = ns
    if why:
        problems.append(why)
    elif ns != NS_APP:
        ctx["notes"].append(f"your objects name namespace {ns}: the marker deploys into {ns}")
    spec = pod_spec(dep)
    cms = {name_of(d) for d in of_kind(ctx["docs"], "ConfigMap")}
    for cont in all_containers(spec):
        for name in sorted(config_map_refs(cont, spec) - cms):
            problems.append(f"container '{cont.get('name')}' reads ConfigMap '{name}', which is not "
                            f"in your path - its Pods could never start")
        for name, key, optional, how in secret_refs(cont, spec):
            if name != SECRET_NAME and not optional:
                problems.append(f"container '{cont.get('name')}' reads Secret '{name}' ({how}); only "
                                f"'{SECRET_NAME}' exists in the marker's cluster - its Pods could "
                                f"never start")
    detail = f"{ctx['source_type']} source, Deployment {name_of(dep)}, image {c.get('image')}"
    return problems, detail


def static_heals(ctx):
    problems = []
    sp = dig(ctx["app"], "spec", "syncPolicy", default={}) or {}
    auto = sp.get("automated") if isinstance(sp, dict) else None
    if not isinstance(auto, dict):
        problems.append("no syncPolicy.automated - Argo CD would only show OutOfSync and wait for "
                        "someone to press Sync. Pulled automatically is a GitOps principle")
    else:
        if auto.get("enabled") is False:
            problems.append("syncPolicy.automated.enabled is false - automated sync is switched off")
        if auto.get("prune") is not True:
            problems.append("prune is not true - a file you delete from Git would stay running in "
                            "the cluster forever")
        if auto.get("selfHeal") is not True:
            problems.append("selfHeal is not true - a change made by hand in the cluster would stay, "
                            "and Git would quietly stop being the truth")
    replicas = dig(ctx["dep"], "spec", "replicas")
    if as_int(replicas) is None:
        problems.append("the Deployment does not set spec.replicas - with no number in Git, Argo CD "
                        "has nothing to put back when someone scales it by hand")
    if of_kind(ctx["docs"], "HorizontalPodAutoscaler"):
        ctx["notes"].append("an HPA also changes replicas - Argo CD and the HPA may fight over the "
                            "number (the heal test checks that Argo CD restored it)")
    return problems, "automated, prune: true, selfHeal: true; replicas in Git"


# --- build separated from deploy ---------------------------------------------------------

_FLAGS = r"(?:\s+-{1,2}[A-Za-z][\w-]*(?:(?:=|\s+)(?!-)[^\s|;&]+)?)*"
DEPLOY_COMMANDS = [
    (re.compile(r"\bkubectl\b" + _FLAGS + r"\s+(apply|create|replace|patch|edit|delete|scale|"
                r"annotate|label|expose|autoscale|run|set\s+[a-z-]+|"
                r"rollout\s+(?:restart|undo|resume))\b"), "kubectl"),
    (re.compile(r"\bhelm\b" + _FLAGS + r"\s+(install|upgrade|rollback|uninstall|delete)\b"), "helm"),
    (re.compile(r"\bargocd\b" + _FLAGS + r"\s+app\s+(sync|set|create|rollback|delete|patch|"
                r"actions\s+run)\b"), "argocd"),
    (re.compile(r"/api/v1/applications\b"), "the Argo CD API"),
]
CLUSTER_CREDENTIALS = re.compile(
    r"\b(aws\s+eks\s+update-kubeconfig|az\s+aks\s+get-credentials|"
    r"gcloud\s+container\s+clusters\s+get-credentials|doctl\s+kubernetes\s+cluster\s+kubeconfig|"
    r"yc\s+managed-kubernetes\s+cluster\s+get-credentials)\b")
SECRET_CREDENTIALS = re.compile(r"secrets\.([A-Za-z0-9_]*(?:KUBE|K8S|CLUSTER|ARGO)[A-Za-z0-9_]*)",
                                re.I)
DRY_RUN = re.compile(r"--dry-run\b|--server-dry-run\b")
TEST_CLUSTER = re.compile(r"\bkind\s+create\s+cluster\b|\bk3d\s+cluster\s+create\b|"
                          r"\bminikube\s+start\b|mark_m[45]\.py\b[^\n]*\s--kind\b")
TEST_CLUSTER_ACTIONS = re.compile(r"^(helm/kind-action|engineerd/setup-kind|medyagh/setup-minikube|"
                                  r"AbsaOSS/k3d-action|nolar/setup-k3d-k3s)@", re.I)
# Actions that only install or check a tool are not deploys; neither are GitHub Pages.
NOT_DEPLOY_ACTIONS = re.compile(r"(?i)(setup|install|lint|tools|chart-testing|chart-releaser|"
                                r"kind-action|bake|pages)")
DEPLOY_ACTIONS = re.compile(r"(deploy|kubectl|k8s|kube|helm|argo|aks-set-context|gke-credentials|"
                            r"ssh-action|scp-action|ssh-deploy)", re.I)
PUSH_COMMANDS = re.compile(r"\b(docker|podman|buildah)\s+(image\s+)?push\b|"
                           r"\bdocker\s+buildx\s+build\b[^\n]*--push\b|\bcrane\s+push\b|"
                           r"\bko\s+build\b")
SCRIPT_CALL = re.compile(r"(?:^|[\s;&|(])(?:bash|sh|zsh|source|\.)\s+([\w./-]+)|"
                         r"(?:^|[\s;&|(])(\./[\w./-]+|scripts/[\w./-]+)")


def joined_lines(text):
    """Shell lines with backslash continuations joined, comments dropped."""
    out, cur = [], ""
    for line in (text or "").splitlines():
        s = line.rstrip()
        if s.endswith("\\"):
            cur += s[:-1] + " "
            continue
        cur += s
        if cur.strip() and not cur.strip().startswith("#"):
            out.append(re.sub(r"\s+", " ", cur.strip()))
        cur = ""
    if cur.strip():
        out.append(re.sub(r"\s+", " ", cur.strip()))
    return out


def deploy_hits(text):
    hits = []
    for line in joined_lines(text):
        if DRY_RUN.search(line):
            continue
        for pat, tool in DEPLOY_COMMANDS:
            m = pat.search(line)
            if m:
                hits.append(short(line, 90))
                break
    return hits


def workflow_files(root):
    wdir = os.path.join(root, ".github", "workflows")
    if not os.path.isdir(wdir):
        return {}
    return {f".github/workflows/{f}": read_text(os.path.join(wdir, f))
            for f in sorted(os.listdir(wdir)) if f.endswith((".yml", ".yaml"))}


def scripts_called(root, run_text):
    """Files in the repository that a run: block executes (scripts, a Makefile)."""
    found = []
    for line in joined_lines(run_text):
        for m in SCRIPT_CALL.finditer(line):
            p = (m.group(1) or m.group(2) or "").strip()
            p = p[2:] if p.startswith("./") else p
            full = os.path.join(root, p)
            if p and os.path.isfile(full) and (os.path.realpath(full)).startswith(os.path.realpath(root)):
                found.append(p)
        if re.search(r"(^|[\s;&|])make\b", line) and os.path.isfile(os.path.join(root, "Makefile")):
            found.append("Makefile")
    return list(dict.fromkeys(found))


def scan_workflows(root):
    """-> (problems, pushes, notes). Every job of every workflow, and the scripts it runs."""
    problems, pushes, notes = [], [], []
    files = workflow_files(root)
    for rel, text in files.items():
        for m in SECRET_CREDENTIALS.finditer(text):
            problems.append(f"{rel} uses secrets.{m.group(1)} - cluster credentials in CI mean CI "
                            f"can change the cluster. Argo CD pulls; CI needs no key to the cluster")
        try:
            wf = yaml.safe_load(text)
        except yaml.YAMLError:
            wf = None
        jobs = wf.get("jobs") if isinstance(wf, dict) else None
        if not isinstance(jobs, dict):
            for h in deploy_hits(text):
                problems.append(f"{rel}: {h}")
            for line in joined_lines(text):
                if CLUSTER_CREDENTIALS.search(line):
                    problems.append(f"{rel}: {short(line, 90)} fetches cluster credentials")
            if PUSH_COMMANDS.search(text):
                pushes.append(rel)
            continue
        for jid, job in jobs.items():
            if not isinstance(job, dict):
                continue
            steps = [s for s in (job.get("steps") or []) if isinstance(s, dict)]
            runs = "\n".join(str(s.get("run") or "") for s in steps)
            uses = [str(s.get("uses") or "") for s in steps if s.get("uses")]
            test_job = bool(TEST_CLUSTER.search(runs) or any(TEST_CLUSTER_ACTIONS.match(u) for u in uses))
            where = f"{rel} job '{jid}'"
            for s in steps:
                u = str(s.get("uses") or "")
                if u.startswith("docker/build-push-action@"):
                    push = (s.get("with") or {}).get("push")
                    if push is True or (isinstance(push, str) and push.strip().lower() not in ("false", "")):
                        pushes.append(f"{rel} job '{jid}'")
                if u.startswith("./"):
                    adir = os.path.join(root, u[2:].split("@")[0])
                    for an in ("action.yml", "action.yaml"):
                        ap = os.path.join(adir, an)
                        if os.path.isfile(ap):
                            try:
                                action = yaml.safe_load(read_text(ap)) or {}
                            except yaml.YAMLError:
                                action = {}
                            asteps = dig(action, "runs", "steps", default=[]) or []
                            atext = "\n".join(str(x.get("run") or "") for x in asteps if isinstance(x, dict))
                            if not test_job:
                                for h in deploy_hits(atext):
                                    problems.append(f"{where}: the local action {u} runs: {h}")
                elif u and not test_job and not TEST_CLUSTER_ACTIONS.match(u) \
                        and not u.lower().startswith(("docker/", "actions/")) \
                        and not NOT_DEPLOY_ACTIONS.search(u.split("@")[0]) \
                        and DEPLOY_ACTIONS.search(u.split("@")[0]):
                    problems.append(f"{where} uses {u.split('@')[0]} - a deploy action in CI. Argo CD "
                                    f"deploys; CI builds and pushes")
            for line in joined_lines(runs):
                if CLUSTER_CREDENTIALS.search(line):
                    problems.append(f"{where}: {short(line, 90)} fetches cluster credentials")
            if PUSH_COMMANDS.search(runs):
                pushes.append(f"{rel} job '{jid}'")
            if test_job:
                notes.append(f"{where} creates its own throwaway cluster to test your files - "
                             f"that is testing, not deploying")
                continue
            for h in deploy_hits(runs):
                problems.append(f"{where}: {h} - CI must not deploy: commit the change to Git and "
                                f"let Argo CD pull it")
            for script in scripts_called(root, runs):
                for h in deploy_hits(read_text(os.path.join(root, script))):
                    problems.append(f"{where} runs {script}, which deploys: {h}")
    return list(dict.fromkeys(problems)), list(dict.fromkeys(pushes)), notes, files


def static_separated(ctx):
    problems, pushes, notes, files = scan_workflows(ctx["root"])
    ctx["notes"].extend(notes)
    if not files:
        return ["there are no workflows in .github/workflows - M3's build-and-push job must still "
                "be there"], ""
    if not pushes:
        problems.append("no workflow builds and pushes your image any more (docker/build-push-action "
                        "with push: true, or docker push) - M3's publish job must stay: CI builds, "
                        "Argo CD deploys")
    return problems, (f"CI builds and pushes ({pushes[0]}); nothing in CI deploys"
                      if pushes else "")


# --- pinned and reversible ---------------------------------------------------------------

def readme_rollback(root):
    """-> (ok, problem text)."""
    names = [f for f in sorted(os.listdir(root))
             if re.fullmatch(r"(?i)readme(\.md|\.markdown|\.rst|\.txt)?", f) and os.path.isfile(os.path.join(root, f))]
    if not names:
        return False, "there is no README.md at the top of your repository"
    lines = read_text(os.path.join(root, names[0])).splitlines()
    heads = []
    for i, line in enumerate(lines):
        m = re.match(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if m:
            heads.append((i, len(m.group(1)), m.group(2)))
        elif i + 1 < len(lines) and line.strip() and re.fullmatch(r"\s{0,3}(=+|-+)\s*", lines[i + 1]):
            heads.append((i, 1 if lines[i + 1].strip().startswith("=") else 2, line.strip()))
    roll = [(i, lvl) for i, lvl, t in heads if re.search(r"(?i)\broll[\s-]?back|\brolling\s+back", t)]
    if not roll:
        return False, (f"{names[0]} has no Rollback section - add a heading such as '## Rollback' "
                       f"that says how you roll back: git revert <commit>, push, and Argo CD syncs "
                       f"the previous version")
    start, lvl = roll[0]
    end = next((i for i, l2, _ in heads if i > start and l2 <= lvl), len(lines))
    section = "\n".join(lines[start:end])
    if not re.search(r"\bgit\s+revert\b", section):
        return False, (f"{names[0]}'s Rollback section does not name git revert - in GitOps you roll "
                       f"back with a new commit that undoes the bad one")
    return True, ""


def image_bumps(ctx):
    """Commits on the marked history that moved a pinned image version (a SHA tag or a digest)
    to another one, inside the Application's path or its value files."""
    paths = [ctx["source_path"]] + [v for v in ctx["value_files"] + ctx["extra_paths"]
                                     if not (v + "/").startswith(ctx["source_path"] + "/")]
    rev = ctx["sha"] or "HEAD"
    # Every commit on the main line (a merged pull request is ONE merge commit there), each
    # compared with its first parent - what that commit changed on the main line.
    rc, out, _ = run(["git", "rev-list", "--first-parent", "--max-count=400",
                      "--format=%H%x09%cI%x09%s", rev], cwd=ctx["repo_root"], timeout=120)
    bumps = []
    for line in out.splitlines() if rc == 0 else []:
        if line.startswith("commit ") or "\t" not in line:
            continue
        sha, date, subject = (line.split("\t") + ["", ""])[:3]
        rc2, diff, _ = run(["git", "diff", "--unified=0", f"{sha}^1", sha, "--", *paths],
                           cwd=ctx["repo_root"], timeout=60)
        if rc2 != 0:
            continue                                   # the first commit: nothing to compare
        removed, added = set(), set()
        for d in diff.splitlines():
            if d.startswith(("---", "+++")) or not re.search(r"(?i)image|tag|digest", d):
                continue
            if d.startswith("-"):
                removed.update(PINNED.findall(d))
            elif d.startswith("+"):
                added.update(PINNED.findall(d))
        new = [v for v in added - removed if v.startswith("sha256:") or is_commit(ctx["repo_root"], v)]
        if removed and new:
            bumps.append((sha[:7], date[:10], subject))
    return bumps


def static_pinned(ctx):
    problems = []
    for d in of_kind(ctx["docs"], "Deployment"):
        for c in all_containers(pod_spec(d)):
            image = c.get("image") or ""
            repo, tag, digest = split_image(image)
            if is_rescue(repo.lower()):
                problems.append(f"container '{c.get('name')}' runs the course's rescue image: this "
                                f"block needs YOUR image, pinned to one of your commits")
                continue
            if digest and re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                if tag == "latest":
                    ctx["notes"].append(f"{image}: the digest pins it; drop ':latest', it only "
                                        f"confuses readers")
                continue
            if not tag:
                problems.append(f"image {image!r} has no tag - that means :latest, which moves")
            elif tag == "latest":
                problems.append(f"image {image!r}: latest is not a version - pin the full commit SHA "
                                f"your M3 pipeline tagged")
            else:
                core = tag[4:] if tag.startswith("sha-") else tag
                if re.fullmatch(r"[0-9a-f]{7,39}", core):
                    problems.append(f"image tag {tag} is a short SHA - use the full 40-character "
                                    f"SHA (git log -1 --format=%H)")
                elif not re.fullmatch(r"[0-9a-f]{40}", core):
                    problems.append(f"image tag {tag!r} is not a commit SHA - a name like that can be "
                                    f"moved to another build; pin the full SHA or a digest")
                elif not is_commit(ctx["repo_root"], core):
                    problems.append(f"image tag {tag} is not a commit in your repository - copy the "
                                    f"SHA from git log, do not type it")
    ok, why = readme_rollback(ctx["root"])
    if not ok:
        problems.append(why)
    bumps = image_bumps(ctx)
    ctx["bumps"] = bumps
    if not bumps:
        problems.append(f"no image bump in your history: no commit (before the deadline) changed the "
                        f"image tag in {ctx['source_path']} from one pinned version to another. Deploy "
                        f"a newer image through Git: change the tag, commit, push")
    detail = ""
    if not problems:
        b = bumps[0]
        detail = (f"every image pinned; README names git revert; {len(bumps)} image bump(s), the "
                  f"latest {b[0]} on {b[1]} ({short(b[2], 50)})")
    return problems, detail


STATIC = {"it syncs": static_syncs, "it heals": static_heals,
          "build is separated from deploy": static_separated,
          "the version is pinned and reversible": static_pinned}
BLOCK_NAMES = list(STATIC)
LIVE_BLOCKS = ("it syncs", "it heals")


# ================================================================== the cluster

class Cluster:
    """The kubectl and kind commands the live checks need. The tests replace the programs
    with fakes on PATH, so every command below is exercised."""

    def __init__(self, context, clock=None):
        self.context = context
        self.clock = clock or Clock()

    def kubectl(self, *args, input=None, timeout=90):
        return run(["kubectl", "--context", self.context, *args], input=input, timeout=timeout)

    def get_json(self, *args, timeout=60):
        rc, out, err = self.kubectl(*args, "-o", "json", timeout=timeout)
        if rc != 0:
            return None
        try:
            return json.loads(out)
        except ValueError:
            return None

    def reachable(self):
        rc, out, _ = self.kubectl("get", "--raw", "/readyz", timeout=30)
        return rc == 0 and "ok" in out

    def apply_obj(self, obj, ns=None):
        args = (["-n", ns] if ns else []) + ["apply", "-f", "-"]
        return self.kubectl(*args, input=json.dumps(obj))

    def namespace(self, ns, psa=True):
        labels = {"pod-security.kubernetes.io/enforce": "baseline",
                  "pod-security.kubernetes.io/enforce-version": "latest",
                  "pod-security.kubernetes.io/warn": "restricted",
                  "pod-security.kubernetes.io/warn-version": "latest"} if psa else {}
        return self.apply_obj({"apiVersion": "v1", "kind": "Namespace",
                               "metadata": {"name": ns, "labels": labels}})

    def install(self, manifest):
        return self.kubectl("-n", NS_ARGO, "apply", "--server-side", "--force-conflicts",
                            "-f", manifest, timeout=INSTALL_SECONDS)

    def crds(self):
        return self.kubectl("wait", "--for", "condition=established", "--timeout=120s",
                            "crd/applications.argoproj.io", "crd/appprojects.argoproj.io",
                            timeout=150)

    def rollout(self, ns, what, seconds):
        return self.kubectl("-n", ns, "rollout", "status", what, f"--timeout={seconds}s",
                            timeout=seconds + 30)

    def app(self):
        return self.get_json("-n", NS_ARGO, "get", "application", APP_NAME)

    def obj(self, ns, kind, name):
        return self.get_json("-n", ns, "get", kind.lower(), name)

    def exists(self, ns, kind, name):
        """True / False, or None when the API did not answer."""
        rc, out, _ = self.kubectl("-n", ns, "get", kind.lower(), name, "--ignore-not-found",
                                  "-o", "name", timeout=30)
        return None if rc != 0 else bool(out.strip())

    def scale(self, ns, name, replicas):
        return self.kubectl("-n", ns, "scale", f"deployment/{name}", f"--replicas={replicas}")

    def delete(self, ns, kind, name):
        return self.kubectl("-n", ns, "delete", kind.lower(), name, "--wait=true",
                            "--timeout=60s", timeout=90)

    def pods(self, ns, selector):
        data = self.get_json("-n", ns, "get", "pods", "-l", selector)
        items = (data or {}).get("items") or []
        return [p for p in items if not dig(p, "metadata", "deletionTimestamp")]

    def events(self, ns):
        data = self.get_json("-n", ns, "get", "events")
        items = (data or {}).get("items") or []
        return sorted(items, key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "")

    def logs(self, ns, pod, container, previous=False):
        args = ["-n", ns, "logs", pod, "-c", container, "--tail=15"]
        if previous:
            args.append("--previous")
        rc, out, err = self.kubectl(*args, timeout=30)
        return short(out if rc == 0 else "", 300)


class Kind:
    def exists(self, name):
        rc, out, _ = run(["kind", "get", "clusters"], timeout=30)
        return rc == 0 and name in out.split()

    def create(self, name):
        return run(["kind", "create", "cluster", "--name", name, "--image", KIND_NODE_IMAGE,
                    "--wait", "180s"], timeout=600)

    def delete(self, name):
        run(["kind", "delete", "cluster", "--name", name], timeout=240)


def diagnose(cl, ns, selector, container_name):
    """One line on why the Pods are not ready: the words from Kubernetes itself."""
    pods = cl.pods(ns, selector)
    if not pods:
        evs = [e for e in cl.events(ns) if e.get("type") == "Warning"]
        return ("no Pods were created" + (f": {short(evs[-1].get('message'), 200)}" if evs else ""))
    bad = [p for p in pods if not pod_ready(p)] or pods
    p = bad[0]
    pname = name_of(p)
    cs = container_status(p, container_name)
    waiting = dig(cs, "state", "waiting", default={}) or {}
    last = dig(cs, "lastState", "terminated", default={}) or {}
    evs = [e for e in cl.events(ns) if dig(e, "involvedObject", "name") == pname
           and e.get("type") == "Warning"]
    ev = short(evs[-1].get("message"), 220) if evs else ""
    phase = dig(p, "status", "phase", default="?")
    if phase == "Pending" and not cs:
        return f"Pod {pname} is Pending{': ' + ev if ev else ''}"
    reason = waiting.get("reason") or (dig(cs, "state", "terminated", "reason")) or \
        ("Running but not ready" if dig(cs, "state", "running") else phase)
    parts = [f"Pod {pname}: {reason}"]
    if waiting.get("message"):
        parts.append(short(waiting["message"], 220))
    elif ev:
        parts.append(ev)
    if last:
        parts.append(f"last exit code {last.get('exitCode')} ({last.get('reason')})")
        logs = cl.logs(ns, pname, container_name, previous=True)
        if logs:
            parts.append(f"its last output: {logs}")
    return " - ".join(parts)


def fetch_manifest(path=None):
    """The pinned Argo CD manifest, checked against its SHA-256. -> (file, problem)."""
    try:
        if path:
            with open(path, "rb") as f:
                data = f.read()
        else:
            with urllib.request.urlopen(ARGOCD_MANIFEST_URL, timeout=90) as r:
                data = r.read()
    except OSError as exc:
        return None, f"could not download the Argo CD manifest: {short(str(exc), 160)}"
    got = hashlib.sha256(data).hexdigest()
    if got != ARGOCD_MANIFEST_SHA256:
        return None, (f"the Argo CD {ARGOCD_VERSION} manifest has SHA-256 {got[:16]}..., not the "
                      f"pinned {ARGOCD_MANIFEST_SHA256[:16]}... - refusing to install it")
    fd, tmp = tempfile.mkstemp(prefix="argocd-", suffix=".yaml")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return tmp, ""


# ================================================================ the live checks

# Messages that mean "the network or a registry failed us", not "your files are wrong".
INFRA = re.compile(r"(?i)(i/o timeout|dial tcp|tls handshake timeout|no such host|connection reset|"
                   r"temporary failure in name resolution|toomanyrequests|429 too many requests|"
                   r"context deadline exceeded|unexpected eof|service unavailable)")
STUDENT_REPO = re.compile(r"(?i)(app path does not exist|app path is not a directory|"
                          r"repository not found|authentication required|unable to resolve|"
                          r"invalid username or password|multiple application sources)")
STUCK_POD = re.compile(r"(ImagePullBackOff|ErrImagePull|CreateContainerConfigError|"
                       r"InvalidImageName|CrashLoopBackOff)")


def marker_application(ctx):
    """The marker's OWN Application: the student's validated fields, pinned to the marked
    commit, in the marker's project. Never the student's object itself."""
    src, spec = ctx["source"], ctx["app"].get("spec") or {}
    s = {"repoURL": src["repoURL"], "targetRevision": ctx["sha"], "path": ctx["source_path"]}
    if ctx["source_type"] == "Directory" and dig(src, "directory", "recurse") is True:
        s["directory"] = {"recurse": True}
    if ctx["source_type"] == "Helm":
        h = {k: copy.deepcopy(v) for k, v in (src.get("helm") or {}).items()
             if k in HELM_OPTIONS - {"version", "releaseName"}}
        h["releaseName"] = ctx["release"]
        s["helm"] = h
    auto = dig(spec, "syncPolicy", "automated")
    on = isinstance(auto, dict) and auto.get("enabled") is not False
    # Automated sync is always on, so that 'it syncs' can be checked; prune and selfHeal are
    # exactly the student's - 'it heals' tests them.
    automated = {"prune": bool(on and auto.get("prune") is True),
                 "selfHeal": bool(on and auto.get("selfHeal") is True)}
    opts = [o for o in (dig(spec, "syncPolicy", "syncOptions") or [])
            if isinstance(o, str) and SYNC_OPTIONS_COPIED.match(o)]
    sync = {"automated": automated}
    if opts:
        sync["syncOptions"] = opts
    retry = dig(spec, "syncPolicy", "retry")
    if isinstance(retry, dict) and isinstance(retry.get("limit"), int) and 0 <= retry["limit"] <= 10:
        sync["retry"] = {"limit": retry["limit"]}
    return {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
            "metadata": {"name": APP_NAME, "namespace": NS_ARGO},
            "spec": {"project": PROJECT, "source": s,
                     "destination": {"server": "https://kubernetes.default.svc",
                                     "namespace": ctx.get("ns") or NS_APP},
                     "syncPolicy": sync}}


def marker_project(ctx):
    """Only this repository, only one namespace, only the M4 kinds, nothing cluster-wide."""
    return {"apiVersion": "argoproj.io/v1alpha1", "kind": "AppProject",
            "metadata": {"name": PROJECT, "namespace": NS_ARGO},
            "spec": {"description": "INF 345 M5 marker: one repository, one namespace, the M4 kinds",
                     "sourceRepos": [ctx["source"]["repoURL"]],
                     "destinations": [{"server": "https://kubernetes.default.svc",
                                       "namespace": ctx.get("ns") or NS_APP}],
                     "clusterResourceWhitelist": [],
                     "namespaceResourceWhitelist": [{"group": g, "kind": k}
                                                    for k, g in sorted(ALLOWED.items())]}}


def app_state(app):
    st = (app or {}).get("status") or {}
    op = st.get("operationState") or {}
    return {"sync": dig(st, "sync", "status", default="Unknown"),
            "revision": dig(st, "sync", "revision", default=""),
            "health": dig(st, "health", "status", default="Unknown"),
            "health_msg": dig(st, "health", "message", default=""),
            "conditions": [(c.get("type"), c.get("message") or "") for c in st.get("conditions") or []
                           if isinstance(c, dict)],
            "op_phase": op.get("phase") or "", "op_msg": op.get("message") or "",
            "op_started": op.get("startedAt") or "",
            "op_automated": bool(dig(op, "operation", "initiatedBy", "automated")),
            "resources": st.get("resources") or []}


class Live:
    """Runs the cluster part once and remembers what it saw, for the blocks."""

    def __init__(self, ctx, cluster, clock=None, rng=None, manifest=None):
        self.ctx, self.cl = ctx, cluster
        self.clock = clock or cluster.clock
        self.rng = rng or random.Random()
        self.manifest = manifest
        self.ns = ctx.get("ns") or NS_APP
        self.dep, self.c = ctx["dep"], ctx["container"]
        self.dep_name = name_of(self.dep)
        self.selector = label_selector(self.dep)
        self.replicas = as_int(dig(self.dep, "spec", "replicas"), 1)
        self.infra = None          # set when the cluster itself failed us
        self.synced = None         # (True | False | None, detail, seconds)
        self.was_synced = False    # Synced at the marked commit at least once (healthy or not)
        self.as_checked = None     # (ok, detail)
        self.secret_values = {}

    # --------------------------------------------------------------- setup
    def install_argocd(self):
        manifest, why = fetch_manifest(self.manifest)
        if not manifest:
            return why
        try:
            rc, _, err = self.cl.namespace(NS_ARGO, psa=False)
            if rc != 0:
                return f"could not create namespace {NS_ARGO}: {short(err)}"
            rc, _, err = self.cl.install(manifest)
            if rc != 0:
                return f"could not install Argo CD {ARGOCD_VERSION}: {short(err, 300)}"
        finally:
            os.remove(manifest)                    # always the checked copy, never reused
        rc, _, err = self.cl.crds()
        if rc != 0:
            return f"Argo CD's CRDs were not ready: {short(err)}"
        for what in ("statefulset/argocd-application-controller", "deployment/argocd-repo-server",
                     "deployment/argocd-redis"):
            rc, _, err = self.cl.rollout(NS_ARGO, what, INSTALL_SECONDS)
            if rc != 0:
                return f"Argo CD's {what} was not ready after {INSTALL_SECONDS} s: {short(err)}"
        return None

    def setup(self):
        if not self.cl.reachable():
            self.infra = "the cluster did not answer"
            return
        why = self.install_argocd()
        if why:
            self.infra = why
            return
        rc, _, err = self.cl.namespace(self.ns)
        if rc != 0:
            self.infra = f"could not create namespace {self.ns}: {short(err)}"
            return
        secret = secret_object(secret_keys(self.c, pod_spec(self.dep)))
        self.secret_values = dict(secret["stringData"])
        rc, _, err = self.cl.apply_obj(secret, self.ns)
        if rc != 0:
            self.infra = f"could not create {SECRET_NAME}: {short(err)}"
            return
        for obj in (marker_project(self.ctx), marker_application(self.ctx)):
            rc, _, err = self.cl.apply_obj(obj)
            if rc != 0:
                self.infra = f"could not create the marker's {obj['kind']}: {short(err)}"
                return
        self.synced = self.wait_synced()
        if self.was_synced:
            self.as_checked = self.check_as_checked()

    def wait_synced(self):
        """-> (True, detail, s) | (False, why, s) | (None, why, s) when the cause is not yours."""
        t0, sha = self.clock.now(), self.ctx["sha"]
        polls = bad_repo = stuck = 0
        st, pods, msgs = {}, "", ""
        while True:
            st = app_state(self.cl.app())
            secs = int(self.clock.now() - t0)
            polls += 1
            if st["sync"] == "Synced" and st["revision"] == sha:
                self.was_synced = True
                if st["health"] == "Healthy":
                    return True, f"Synced and Healthy at {sha[:7]} after about {secs} s", secs
            msgs = " ".join(m for _, m in st["conditions"]) + " " + st["op_msg"]
            bad_repo = bad_repo + 1 if STUDENT_REPO.search(msgs) and not INFRA.search(msgs) else 0
            if bad_repo >= 5:
                return False, (f"Argo CD could not build your manifests (ComparisonError): "
                               f"{short(msgs, 300)}"), secs
            if self.was_synced and polls % 10 == 0 and self.selector:
                pods = diagnose(self.cl, self.ns, self.selector, self.c.get("name"))
                stuck = stuck + 1 if STUCK_POD.search(pods) else 0
                if stuck >= 2 and not INFRA.search(pods):
                    break                  # the same Pod problem for a minute: it will not heal
            if st["health"] == "Degraded" or secs > SYNC_SECONDS:
                break
            self.clock.sleep(POLL_SECONDS)
        if self.selector and not pods:
            pods = diagnose(self.cl, self.ns, self.selector, self.c.get("name"))
        if INFRA.search(msgs) or re.search(r"(?i)toomanyrequests|429 too many", pods):
            return None, f"GitHub or a registry did not answer: {short(msgs + ' ' + pods, 300)}", secs
        cond = "; ".join(f"{t}: {short(m, 160)}" for t, m in st["conditions"])
        why = (f"after {secs} s Argo CD says {st['sync']} and {st['health']}"
               + (f" ({short(st['health_msg'], 120)})" if st["health_msg"] else "")
               + (f"; {cond}" if cond else "")
               + (f"; last sync {st['op_phase']}: {short(st['op_msg'], 200)}" if st["op_msg"] else "")
               + (f"; {pods}" if pods else ""))
        if self.was_synced:
            why += (" - the cluster matches Git, and Git describes an app that does not become "
                    "ready: fix it in Git (M4)")
        return False, why, secs

    def check_as_checked(self):
        """Argo CD must have deployed exactly what the marker rendered and checked."""
        rendered = {(group_of(d), d.get("kind"), name_of(d)) for d in self.ctx["docs"]}
        st = app_state(self.cl.app())
        live = {(r.get("group") or "", r.get("kind"), r.get("name")) for r in st["resources"]
                if isinstance(r, dict) and not r.get("hook")}
        extra = sorted(live - rendered)
        if extra:
            g, k, n = extra[0]
            return False, (f"Argo CD deployed {k} {n}, which the marker's rendering did not have - "
                           f"what is deployed must be what was checked")
        problems = []
        for n in sorted({n for _, k, n in live if k == "Deployment"}):
            obj = self.cl.obj(self.ns, "deployment", n)
            if obj is None:
                continue
            obj["_file"] = f"live Deployment {n}"
            ok, why = gate_safe(Ctx(docs=[obj]))
            if not ok:
                problems.append(why)
            for cont in all_containers(pod_spec(obj)):
                repo = split_image(cont.get("image"))[0]
                if repo != self.ctx["expected_repo"] and not is_rescue(repo.lower()):
                    problems.append(f"live Deployment {n} runs {cont.get('image')}, which is not "
                                    f"your image")
        if problems:
            return False, "; ".join(problems)
        missing = sorted(rendered - live)
        if missing:
            self.ctx["notes"].append(f"Argo CD does not list {missing[0][1]} {missing[0][2]} as one "
                                     f"of its resources")
        return True, f"Argo CD deployed exactly the {len(live)} object(s) the marker checked"

    def automated_since(self, before):
        st = app_state(self.cl.app())
        return st["op_automated"] and st["op_started"] > before

    def settle(self):
        """Wait (a little) until the Application is Synced and no sync is running."""
        t0 = self.clock.now()
        while self.clock.now() - t0 < SETTLE_SECONDS:
            st = app_state(self.cl.app())
            if st["sync"] == "Synced" and st["op_phase"] not in ("Running", "Terminating"):
                return st
            self.clock.sleep(POLL_SECONDS)
        return app_state(self.cl.app())

    # ------------------------------------------------------------ block 1
    def syncs(self):
        ok, detail, _ = self.synced
        if not ok:
            return ok, detail
        live = self.cl.obj(self.ns, "deployment", self.dep_name) or {}
        want = {c.get("name"): c.get("image") for c in all_containers(pod_spec(self.dep))}
        have = {c.get("name"): c.get("image") for c in all_containers(pod_spec(live))}
        if want != have:
            return False, f"the Deployment runs {have}, but Git says {want}"
        sec = self.cl.obj(self.ns, "secret", SECRET_NAME) or {}
        md = sec.get("metadata") or {}
        if TRACKING in (md.get("annotations") or {}) or \
                "app.kubernetes.io/instance" in (md.get("labels") or {}):
            return False, (f"Argo CD manages {SECRET_NAME} - it must not: the Secret is created on the "
                           f"cluster, never in Git")
        data = {k: base64.b64decode(v).decode("utf-8", "replace")
                for k, v in (sec.get("data") or {}).items()}
        if self.secret_values and data != self.secret_values:
            return False, f"{SECRET_NAME} was changed or removed after the marker created it"
        return True, (f"{detail}; the Pods run {self.c.get('image')}; Argo CD left {SECRET_NAME} "
                      f"alone")

    # ------------------------------------------------------------ block 2
    def heal_scale(self):
        before, to = self.settle()["op_started"], self.replicas + 3
        rc, _, err = self.cl.scale(self.ns, self.dep_name, to)
        if rc != 0:
            return None, f"could not scale the Deployment: {short(err)}"
        t0 = self.clock.now()
        while self.clock.now() - t0 <= HEAL_SECONDS:
            self.clock.sleep(POLL_SECONDS)
            dep = self.cl.obj(self.ns, "deployment", self.dep_name) or {}
            if as_int(dig(dep, "spec", "replicas")) == self.replicas:
                secs = int(self.clock.now() - t0)
                if self.automated_since(before):
                    return True, f"scaled to {to} by hand: back to {self.replicas} after about {secs} s"
                return False, (f"replicas went back to {self.replicas}, but not by an Argo CD sync - "
                               f"something else (an HPA?) changed it")
        return False, (f"scaled to {to} by hand: still not {self.replicas} after {HEAL_SECONDS} s - "
                       f"without selfHeal, Argo CD only shows OutOfSync")

    def heal_delete(self):
        svc = name_of(of_kind(self.ctx["docs"], "Service")[0])
        before = self.settle()["op_started"]
        uid = dig(self.cl.obj(self.ns, "service", svc) or {}, "metadata", "uid", default="")
        rc, _, err = self.cl.delete(self.ns, "service", svc)
        if rc != 0:
            return None, f"could not delete Service {svc}: {short(err)}"
        t0 = self.clock.now()
        while self.clock.now() - t0 <= HEAL_SECONDS:
            self.clock.sleep(POLL_SECONDS)
            now = self.cl.obj(self.ns, "service", svc)
            if now and dig(now, "metadata", "uid", default="") != uid and self.automated_since(before):
                return True, (f"deleted Service {svc}: recreated after about "
                              f"{int(self.clock.now() - t0)} s")
        return False, (f"deleted Service {svc}: not recreated after {HEAL_SECONDS} s - selfHeal is "
                       f"what repairs a deleted object")

    def heal_prune(self):
        self.settle()
        probe = {"apiVersion": "v1", "kind": "ConfigMap",
                 "metadata": {"name": PRUNE_PROBE, "namespace": self.ns,
                              "annotations": {TRACKING: f"{APP_NAME}:/ConfigMap:{self.ns}/{PRUNE_PROBE}"}},
                 "data": {"why": "Argo CD thinks it created this from Git. Git does not have it. "
                                 "With prune: true, Argo CD deletes it."}}
        rc, _, err = self.cl.apply_obj(probe, self.ns)
        if rc != 0:
            return None, f"could not create the prune test object: {short(err)}"
        t0 = self.clock.now()
        while self.clock.now() - t0 <= HEAL_SECONDS:
            self.clock.sleep(POLL_SECONDS)
            if self.cl.exists(self.ns, "configmap", PRUNE_PROBE) is False:
                return True, (f"an object Argo CD owned but Git did not have: pruned after about "
                              f"{int(self.clock.now() - t0)} s")
        return False, (f"an object Argo CD owns but Git does not have (ConfigMap {PRUNE_PROBE}) was "
                       f"still there after {HEAL_SECONDS} s - prune: true deletes such objects")

    def heals(self):
        if not self.was_synced:
            return False, "Argo CD never synced your files - see 'it syncs'"
        results = [self.heal_scale(), self.heal_delete(), self.heal_prune()]
        if any(r[0] is False for r in results):
            return False, "; ".join(d for ok, d in results if ok is False)
        if any(r[0] is None for r in results):
            return None, "; ".join(d for ok, d in results if ok is None)
        return True, "; ".join(d for _, d in results)


# ================================================================ marking

def mark(ctx, live_mode, cluster=None, kind=None, cluster_name=None, create=False,
         keep=False, clock=None, rng=None, manifest=None):
    rep = {"repo": ctx["slug"], "commit": {"sha": ctx["sha"], "date": ctx["date"],
                                          "subject": ctx["subject"]},
           "mode": "live" if live_mode else "static", "gates": [], "blocks": [],
           "skipped": [], "score": 0, "capped": False, "notes": ctx["notes"]}
    for name, fn in STATIC_GATES:
        if name == "renders" and ctx.get("source") is not None:
            render(ctx)
        ok, detail = fn(ctx)
        if ok is None:                 # a tool is missing HERE: not a zero, nothing checkable
            rep["gates"].append({"name": name, "ok": None, "detail": f"NOT CHECKED: {detail}"})
            rep["tool_missing"] = True
            for b in BLOCK_NAMES:
                rep["skipped"].append({"name": b, "detail": f"NOT CHECKED: {detail}"})
            return rep
        rep["gates"].append({"name": name, "ok": ok, "detail": detail})
        rep["capped"] = rep["capped"] or not ok
        if not ok and name in ("application", "renders", "manifests"):
            break                      # nothing below can be read
    if rep["capped"]:
        return rep
    static = {name: STATIC[name](ctx) for name in BLOCK_NAMES}

    def score(name, ok, detail):
        rep["blocks"].append({"name": name, "points": 25 if ok else 0, "detail": detail})
        rep["score"] += 25 if ok else 0

    for name in BLOCK_NAMES:
        problems, detail = static[name]
        if problems:
            score(name, False, "; ".join(problems))
        elif name not in LIVE_BLOCKS:
            score(name, True, detail)
        elif not live_mode:
            rep["skipped"].append({"name": name, "detail": f"static checks pass ({detail}); live "
                                                          f"checks not run - no cluster"})
    if not live_mode:
        rep["notes"].append("live checks not run: they need Docker, kind and kubectl (--kind), "
                            "or your pull-request job")
        return _order(rep)
    if all(n in {b["name"] for b in rep["blocks"]} for n in LIVE_BLOCKS):
        return _order(rep)             # both live blocks already failed from the files alone
    live, created = None, False
    try:
        if create:
            print(f"  creating a throwaway kind cluster '{cluster_name}' and installing Argo CD "
                  f"{ARGOCD_VERSION} (a few minutes)...", file=sys.stderr)
            rc, _, err = kind.create(cluster_name)
            if rc != 0:
                return _not_checked(rep, f"could not create the kind cluster: {short(err, 200)}")
            created = True
        live = Live(ctx, cluster, clock=clock, rng=rng, manifest=manifest)
        live.setup()
        if live.infra:
            return _not_checked(rep, live.infra)
        if live.synced and live.synced[0] is None:
            return _not_checked(rep, live.synced[1])
        if live.as_checked is not None:
            ok, detail = live.as_checked
            rep["gates"].append({"name": "as checked", "ok": ok, "detail": detail})
            if not ok:
                rep["capped"] = True
                rep["score"] = 0
                rep["blocks"] = []
                return rep
        for name, fn in (("it syncs", live.syncs), ("it heals", live.heals)):
            if name in [b["name"] for b in rep["blocks"]]:
                continue               # its static part already failed
            ok, detail = fn()
            if ok is None:
                rep["skipped"].append({"name": name, "detail": f"NOT CHECKED: {detail}"})
            else:
                score(name, ok, detail)
        return _order(rep)
    finally:
        if created and not keep:
            kind.delete(cluster_name)


def _order(rep):
    pos = {n: i for i, n in enumerate(BLOCK_NAMES)}
    rep["blocks"].sort(key=lambda b: pos[b["name"]])
    rep["skipped"].sort(key=lambda b: pos[b["name"]])
    return rep


def _not_checked(rep, why):
    done = {b["name"] for b in rep["blocks"]}
    for name in LIVE_BLOCKS:
        if name not in done:
            rep["skipped"].append({"name": name, "detail": f"NOT CHECKED: {why}"})
    return _order(rep)


def print_report(ctx, rep):
    print(f"\n  M5 for {ctx['slug'] or ctx['root']}  ({rep['mode']} checks)")
    if ctx["sha"]:
        print(f"  commit {ctx['sha'][:7]}  {ctx['date']}  {(ctx['subject'] or '')[:50]}")
    print()
    for g in rep["gates"]:
        flag = "ok " if g["ok"] else ("-- " if g["ok"] is None else "FAIL")
        print(f"  [gate {flag}] {g['name']}: {g['detail']}")
    if rep.get("tool_missing"):
        print("\n  Nothing could be checked on this machine - see the gate above.")
        return
    if rep["capped"]:
        print("\n  A gate failed, so this milestone scores 0.\n\nSCORE: 0/100")
        return
    print()
    results = {b["name"]: b for b in rep["blocks"] + rep["skipped"]}
    for name in BLOCK_NAMES:
        b = results.get(name)
        if not b:
            continue
        pts = f"{b['points']:>2}" if "points" in b else "--"
        print(f"  [ {pts}/25 ] {name}")
        print(f"            {b['detail']}")
    for n in rep["notes"]:
        print(f"  note: {n}")
    if rep["mode"] == "static":
        zeros = [b["name"] for b in rep["blocks"] if b["points"] == 0]
        if zeros:
            print(f"\nSTATIC: {len(zeros)} block(s) already fail from the files alone. Fix them, "
                  f"then run with --kind (or push and let your pull-request job run).")
        else:
            print("\nSTATIC: every static check passes. The live checks decide 'it syncs' and "
                  "'it heals': run with --kind, or push and let your pull-request job run.")
    elif rep["skipped"]:
        print(f"\nSCORE: {rep['score']}/{25 * len(rep['blocks'])} checked "
              f"({len(rep['skipped'])} block(s) NOT CHECKED - run it again)")
    else:
        print(f"\nSCORE: {rep['score']}/100")


def exit_code(rep):
    if rep.get("tool_missing"):
        return 2
    if rep["capped"]:
        return 1
    if rep["mode"] == "static":
        return 1 if any(b["points"] == 0 for b in rep["blocks"]) else 0
    return 0 if rep["score"] == 100 and not rep["skipped"] else 1


# ================================================================ main

def commit_info(repo_root, rev="HEAD"):
    rc, out, _ = run(["git", "log", "-1", "--format=%H%x09%cI%x09%s", rev], cwd=repo_root)
    if rc != 0 or not out.strip():
        return "", "", ""
    return tuple((out.strip().split("\t") + ["", "", ""])[:3])


def deadline_commit(repo_root):
    rc, out, _ = run(["git", "rev-list", "-1", "--first-parent", f"--before={DEADLINE}", "HEAD"],
                     cwd=repo_root)
    return out.strip() if rc == 0 and out.strip() else None


def tree_of(repo_root, rev, temps):
    """The files of one commit, in a temporary folder - never the student's working tree.
    A local clone, not `git archive`: archive would leave out files marked export-ignore."""
    root = tempfile.mkdtemp(prefix="m5-tree-")
    temps.append(root)
    run(["git", "clone", "--quiet", "--no-checkout", repo_root, root], timeout=120)
    run(["git", "checkout", "--quiet", "--detach", rev], cwd=root, timeout=120)
    return root


def build_ctx(root, repo_root, slug, sha, date, subject):
    app, app_file, problems, notes, raws = load_application(root)
    expected = f"ghcr.io/{slug}".lower() if slug else None
    return Ctx(root=root, repo_root=repo_root, slug=slug, expected_repo=expected, sha=sha,
               date=date, subject=subject, app=app, app_file=app_file, app_problems=problems,
               app_raws=raws, notes=notes, source=None, source_path=None, source_type=None,
               docs=None, raws={}, render_problems=[], value_files=[], extra_paths=[], release=None,
               dep=None, container=None, bumps=[])


def main(argv=None, clock=None, rng=None):
    ap = argparse.ArgumentParser(description="Mark INF 345 milestone 5.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--repo", help="https://github.com/<user>/<project> - cloned fresh")
    src.add_argument("--path", help="a local checkout")
    ap.add_argument("--kind", action="store_true",
                    help=f"the live checks: a throwaway kind cluster ({KIND_CLUSTER}) with Argo CD")
    ap.add_argument("--keep-cluster", action="store_true", help="do not delete the kind cluster")
    ap.add_argument("--at-deadline", action="store_true",
                    help=f"mark the last commit on or before {DEADLINE}")
    ap.add_argument("--argocd-manifest", metavar="FILE",
                    help="a downloaded core-install.yaml (its SHA-256 is still checked)")
    ap.add_argument("--json", action="store_true", help="print the result as JSON only")
    ap.add_argument("--json-out", metavar="FILE", help="also write the result as JSON to FILE")
    a = ap.parse_args(argv)

    if a.kind:
        missing = [t for t in ("kubectl", "kind", "docker") if not shutil.which(t)]
        if missing:
            print(f"::error::the live checks need {', '.join(missing)} on PATH")
            return 2
    temps = []
    try:
        notes = []
        if a.repo:
            slug = repo_slug(a.repo)
            tmp = tempfile.mkdtemp(prefix="m5-")
            temps.append(tmp)
            rc, _, err = run(["git", "clone", "--quiet", a.repo, tmp], timeout=300)
            if rc != 0:
                print(f"::error::could not clone {a.repo}: {short(err, 200)}")
                return 1
            repo_root = root = tmp
        else:
            repo_root = root = os.path.abspath(a.path)
            rc, out, _ = run(["git", "remote", "get-url", "origin"], cwd=repo_root)
            slug = repo_slug(out.strip()) if rc == 0 else None
        sha, date, subject = commit_info(repo_root)
        if a.at_deadline:
            dc = deadline_commit(repo_root)
            if dc:
                sha, date, subject = commit_info(repo_root, dc)
                notes.append(f"marking the last commit on or before {DEADLINE}")
                if a.repo:
                    run(["git", "checkout", "--quiet", "--detach", dc], cwd=repo_root)
                else:
                    root = tree_of(repo_root, dc, temps)
            else:
                notes.append(f"no commit on or before {DEADLINE} - marking the newest commit")
        elif a.path and a.kind:
            # Argo CD reads GitHub, not this folder: mark the committed files, as it will.
            rc, out, _ = run(["git", "status", "--porcelain"], cwd=repo_root)
            if out.strip():
                notes.append("you have uncommitted changes - the live checks use the last commit, "
                             "because Argo CD reads GitHub, not your laptop")
            root = tree_of(repo_root, "HEAD", temps)
            rc, out, _ = run(["git", "branch", "-r", "--contains", "HEAD"], cwd=repo_root)
            if rc == 0 and not out.strip():
                notes.append("your last commit is not on GitHub yet - push it, or Argo CD cannot "
                             "find it")
        ctx = build_ctx(root, repo_root, slug, sha, date, subject)
        ctx["notes"].extend(notes)
        cluster = kind = None
        if a.kind:
            kind = Kind()
            cluster = Cluster(f"kind-{KIND_CLUSTER}", clock=clock)
        rep = mark(ctx, a.kind, cluster=cluster, kind=kind, cluster_name=KIND_CLUSTER,
                   create=bool(a.kind), keep=a.keep_cluster, clock=clock, rng=rng,
                   manifest=a.argocd_manifest)
        rep["checked_out_of"] = 25 * len(rep["blocks"]) if not rep["capped"] else 100
        if a.json_out:
            with open(a.json_out, "w", encoding="utf-8") as f:
                json.dump(rep, f, indent=2)
        if a.json:
            print(json.dumps(rep, indent=2))
        else:
            print_report(ctx, rep)
        return exit_code(rep)
    finally:
        for t in temps:
            shutil.rmtree(t, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
