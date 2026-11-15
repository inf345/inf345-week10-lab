# INF 345 — Week 10 lab: Argo CD deploys your service from Git — M5

**Tuesday 17 November · Group A 12:30 · Group B 13:30 · on your own laptop**

**M5 is due Sunday 22 November, 23:59** (Almaty time, judged by commit time). The marker,
`mark_m5.py`, is in this repository and it is the rubric: run it on yourself as often as you
like. Helm and Kustomize are optional: your plain M4 `k8s/` folder is enough for M5.

## Tonight: before the lab (20 minutes, big downloads)

If anything fails, email me the error **text** the same evening.

**1. Your cluster works, and M4 is merged**

```bash
kubectl get nodes                  # one node, Ready
kubectl get secret app-secret      # it exists (Week 9)
```

Your M4 `k8s/` folder must be on `main` on GitHub. No cluster on your laptop? Use Killercoda
(killercoda.com → *Playgrounds* → *Kubernetes*); a session lasts one hour, so there you
install Argo CD at the start of the lab, with Path B.

**2. Choose an install: Path A (full, with a web page) or Path B (core, no web page)**

Argo CD runs inside your kind cluster, so Docker's memory is the limit. Path A needs about
6 GB for Docker: a 16 GB laptop is fine. With 8 GB of memory or less, use Path B.
(Windows: WSL 2 gets half of your memory. macOS: Docker Desktop → Settings → Resources.)

**3. Install Argo CD 3.5.3**, the version the marker uses. We pin it and check the file's
fingerprint, exactly like kind and kubectl in Week 8. Never use the `stable` link from old
tutorials: it moves.

Path A — the full install:

```bash
kubectl create namespace argocd
curl -fsSLO https://raw.githubusercontent.com/argoproj/argo-cd/v3.5.3/manifests/install.yaml
echo "7efe2d6bbc03f63623640f1e4198f16c84009d510fb810ef71e56df1b7614ba9  install.yaml" | sha256sum -c -
kubectl apply -n argocd --server-side --force-conflicts -f install.yaml
kubectl -n argocd wait --for=condition=Ready pods --all --timeout=600s
```

Path B — the core install:

```bash
kubectl create namespace argocd
curl -fsSLO https://raw.githubusercontent.com/argoproj/argo-cd/v3.5.3/manifests/core-install.yaml
echo "1a87025d8eb2eae621653fd312fb9ca51df1b4b3b6992a030e3a9ef38e45c448  core-install.yaml" | sha256sum -c -
kubectl apply -n argocd --server-side --force-conflicts -f core-install.yaml
kubectl -n argocd wait --for=condition=Ready pods --all --timeout=600s
```

On macOS, use `shasum -a 256 -c -` instead of `sha256sum -c -`. The check must print `OK`;
if it prints `FAILED`, download the file again and **never apply a file that fails its
check**. `--server-side` is needed since Argo CD 3.3: a plain `kubectl apply` fails with
`Too long: may not be more than 262144 bytes`.

You should see every Pod `Running` and `1/1` in `kubectl -n argocd get pods`: seven for
Path A, four for Path B. The first time takes a few minutes.

**4. The marker, in your project**

```bash
cd ~/<your-project>
curl -fsSLO https://raw.githubusercontent.com/inf345/inf345-week10-lab/main/mark_m5.py
python3 mark_m5.py --help
echo mark_m5.py >> .git/info/exclude          # never commit it
```

**5. This repository**

```bash
git clone https://github.com/inf345/inf345-week10-lab.git ~/inf345-week10-lab
```

---

## In the lab, part 1: Argo CD takes over your service

### Step 1 — Look at Argo CD (5 min)

```bash
kubectl -n argocd get pods
kubectl get crd | grep argoproj
```

Three new kinds now exist: `applications`, `applicationsets` and `appprojects`. You will
write an **Application**: "deploy this folder of this repository into this namespace, and
keep it that way".

**Path A, the web page.** In a second terminal, run
`kubectl -n argocd port-forward service/argocd-server 8080:443`, open
`https://localhost:8080` and accept the certificate warning (Argo CD signed its own
certificate). The user is `admin`; the password:

```bash
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d; echo
```

**Path B** has no web page: you watch with `kubectl`. Everything below works without it.

### Step 2 — Your Application: eight decisions (10 min)

```bash
cd ~/<your-project>
git switch main && git pull && git switch -c m5
mkdir -p argocd
cp ~/inf345-week10-lab/starter/argocd/application.yaml argocd/
```

Fill every `____`. The comment next to each blank is the question:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: ____                     # 1. this app's name in Argo CD, e.g. notes
  namespace: ____                # 2. the namespace where Argo CD looks for Applications
spec:
  project: default               # Argo CD's built-in project (the marker uses a strict one)
  source:
    repoURL: https://github.com/____/____   # 3. YOUR repository: public, https
    targetRevision: ____         # 4. the branch Argo CD follows: the one you merge into
    path: ____                   # 5. the folder with your M4 manifests
  destination:
    server: https://kubernetes.default.svc   # "this cluster": the one Argo CD runs in
    namespace: ____              # 6. where your app runs: where you deployed it in Week 9
  syncPolicy:
    automated:                   # pull automatically: nobody presses "Sync"
      prune: ____                # 7. delete from the cluster what you delete from Git?
      selfHeal: ____             # 8. undo changes made by hand in the cluster?
```

Check it, then commit, push, open a pull request and **merge it**: Argo CD reads `main` on
GitHub, not your laptop.

```bash
python3 mark_m5.py --path .                 # the "application" gate must say ok
git add argocd/application.yaml
git commit -m "M5: an Argo CD Application for k8s/"
git push -u origin m5
```

### Step 3 — Hand your service over to Argo CD (5 min)

Until now *you* ran `kubectl apply`. From now on, Argo CD does. `app-secret` stays: it is
not in `k8s/`.

```bash
git switch main && git pull
kubectl delete -f k8s/
kubectl apply -f argocd/application.yaml    # the only apply you run for your app
kubectl -n argocd get application <name from blank 1> -w
```

Within a minute you should see `Synced` and `Healthy`, and `kubectl get pods` shows your
Pods, which you never applied. **Synced** means "the cluster matches Git"; **Healthy** means
"it works". Synced but not Healthy means Argo CD did its job and Git describes something
broken.

**Apple M-series Mac:** if your Pods show `ImagePullBackOff` with `no match for platform in
manifest`, your image is Intel-only (as in Weeks 8–9). Do the lab on Killercoda or in a
Codespace; the marker runs on Intel machines, so your image is fine for marking.

### Step 4 — Change it by hand, and watch it come back (5 min)

```bash
kubectl scale deployment/<your Deployment> --replicas=5
kubectl get deployment <your Deployment> -w
```

Predict first: five Pods, or your number from Git? Then try
`kubectl delete service <your Service>` and `kubectl get service <your Service> -w`. Both
come back: self-heal repairs any difference from Git. **The way to scale is a commit.**

---

## In the lab, part 2: deploy and roll back through Git

### Step 5 — Deploy a new image by changing one line (10 min)

Merging Step 2 made CI publish a new image, `ghcr.io/<you>/<project>:<commit SHA>`
(Week 6). Deploy it through Git:

```bash
git switch main && git pull
git log -1 --format=%H                      # the full SHA; its Actions run must be green
git switch -c deploy-new-image
# edit k8s/deployment.yaml: replace the 40-character tag with that SHA
git commit -am "Deploy <first 7 characters of the SHA>"
git push -u origin deploy-new-image
```

Open a pull request and merge it. Argo CD looks at GitHub every three minutes; to make it
look now:

```bash
kubectl -n argocd annotate application <name> argocd.argoproj.io/refresh=normal --overwrite
kubectl rollout status deployment/<your Deployment>
kubectl get deployment <your Deployment> -o jsonpath='{..image}'; echo
```

The image now ends in the new SHA: you deployed by merging a pull request, with no password
to the cluster. This commit is the **image bump** the marker looks for.

### Step 6 — Roll back with `git revert` (5 min)

In GitOps you do not run `kubectl rollout undo` (self-heal would put the bad version
back, because Git still says so). You undo the **commit**:

```bash
git switch main && git pull
git log --oneline -3                        # find the merge of "Deploy ..."
git revert --no-edit -m 1 <that merge commit>
git push origin main
kubectl -n argocd annotate application <name> argocd.argoproj.io/refresh=normal --overwrite
kubectl get deployment <your Deployment> -o jsonpath='{..image}'; echo
```

(If you used *Squash and merge*, revert the squashed commit without `-m 1`.) The old SHA is
back, and `git log` shows both commits: your audit trail.

Add this section to your `README.md` and fill the blanks (it is in
`starter/rollback-section.md`):

```markdown
## Rollback

Argo CD deploys what is on the `____` branch, from the `____` folder.
To roll back, undo the commit that deployed the bad version, with a new commit:

    git log --oneline -- ____
    git revert ____
    git push

Argo CD deploys the previous version within three minutes. Do not use
`kubectl rollout undo`: self-heal would put the bad version back.
```

### Step 7 — CI builds, Argo CD deploys; mark yourself (5 min)

Your workflows must still **build and push** your image and must **not deploy**:

```bash
grep -nE "kubectl|helm|argocd" .github/workflows/*.y*ml
```

Delete any step that runs `kubectl apply`, `kubectl set image`, `helm upgrade` or
`argocd app sync` against a real cluster. (A job that makes its own throwaway kind cluster to
*test* your files, like the Week 9 pull-request job, is fine.) Commit the README, push,
merge, then:

```bash
python3 mark_m5.py --path .                 # STATIC: every static check passes.
```

The live checks decide two blocks. Either run `python3 mark_m5.py --path . --kind` on your
laptop (it makes its own cluster, several minutes; yours is not touched), or copy
`examples/m5-pr.yml` to `.github/workflows/m5.yml` in your project and open a pull request
(works on every laptop). You are done when you see `SCORE: 100/100`.

---

## How M5 is marked

The official run uses this `mark_m5.py` with `--kind --at-deadline` on GitHub's machines
(Kubernetes 1.36.4, Argo CD 3.5.3, Helm 4.2.1). It marks the last commit on your default
branch before **Sunday 22 November, 23:59**.

**Gates (any failure means 0):** `argocd/` holds one Application whose `repoURL` is your own
public repository and whose `path` exists; the path renders the way Argo CD would (plain
YAML, a Helm chart or a Kustomize folder); only the M4 kinds (**Argo CD applies everything in
your path**, so a Namespace or an HTTPRoute there now fails), one Deployment, a Service, your
own image (or the rescue image); nothing unsafe; no Secret or secret value anywhere Argo CD
would deploy; no credential in your Git history.

| Block, 25 points each | What must be true |
|---|---|
| it syncs | the marker makes its **own** Application from your fields, pinned to your deadline commit: Synced and Healthy within 300 s, running the image named in Git, and `app-secret` left alone |
| it heals | `automated` with `prune: true` and `selfHeal: true`, and `replicas` in Git. Live: the marker scales your Deployment, deletes your Service and plants an object Git does not have; Argo CD must undo all three |
| build is separated from deploy | a workflow builds and pushes your image; no workflow, and no script a workflow runs, deploys or holds cluster credentials |
| the version is pinned and reversible | every image by its full commit SHA (or a digest); a README section titled **Rollback** that names `git revert`; at least one **image bump** in your history (Step 5) |

**NOT CHECKED** means the marker's cluster, GitHub or a registry did not answer. It is not a
zero, and I re-run it. The rescue image costs only the last block. In the Tuesday sessions I
may ask you about one line ("What does `selfHeal` do? How do you roll back?"). Late work:
10 points per started day, down to 50.

---

## Stuck?

1. `kubectl -n argocd get application <name> -o yaml | tail -40`: read `status` (the
   conditions, `sync`, `health` and `operationState.message`). On the web page it is the
   same text.
2. `python3 mark_m5.py --path .` names the gate or block and the problem.
3. Look up the exact words in *Common failures* below.
4. In the session, paste the error **text** in the chat. After it, email me from your
   `@sdu.edu.kz` address (subject: `INF 345` and your group) with the link to your pull
   request and the output of `mark_m5.py --path .`, as text.

<details>
<summary><b>Common failures</b></summary>

| You see | Fix |
|---|---|
| `metadata.annotations: Too long: may not be more than 262144 bytes` when installing | Install again with `--server-side --force-conflicts` |
| `no matches for kind "Application" in version "argoproj.io/v1alpha1"` | Argo CD is not in the cluster `kubectl` talks to: `kubectl config current-context`, then install it |
| The Application exists but nothing ever happens | It is in the wrong namespace: blank 2 is `argocd` |
| `ComparisonError` … `app path does not exist` | That folder is not on that branch **on GitHub**: a typo, a capital letter, or a branch you did not merge |
| `ComparisonError` … `unable to resolve 'mian' to a commit SHA` | `targetRevision` names a branch that does not exist: `main` |
| `ComparisonError` … `authentication required` | Your repository is private or the URL has a typo: make it public, copy the URL from the browser |
| `Synced` but `Progressing` for minutes; Pods in `ImagePullBackOff` | Git names an image that cannot be pulled: a typo or short SHA, a red CI run, a private package, or an Intel-only image on an Apple M-series Mac. Fix the tag in Git |
| `Synced` and `Healthy`, but your change is not there | You did not merge, or Argo CD has not looked yet: merge, then the `refresh` line from Step 5 |
| A change made with `kubectl edit` is gone a few seconds later | That is self-heal. Change it in Git |
| `app-secret` turned into `REPLACE_ME`, then disappeared | A Secret file is in `k8s/`: Argo CD applied it, and `prune` deleted it later. Remove it from Git and create `app-secret` again on the cluster |
| Objects disappeared after you added `kustomization.yaml` to `k8s/` | Now only the files listed under `resources:` are deployed. List them all, or keep it in another folder |
| Argo CD's Pods are `Pending`, `OOMKilled` or restarting | Not enough memory: `kubectl delete namespace argocd` and use Path B, or give Docker more memory |
| The marker says `no image bump in your history` | Do Step 5. A revert does not remove the bump |
| The marker says `CI must not deploy` | Delete that step from your workflow: Argo CD deploys now |

</details>

<details>
<summary><b>Stretch goals (not marked)</b></summary>

**1. Chart your app with Helm 4.** Install Helm 4.3.0, pinned and checked:

```bash
# Windows (Ubuntu/WSL) and Linux, Intel or AMD:
cd /tmp
curl -fsSLO https://get.helm.sh/helm-v4.3.0-linux-amd64.tar.gz
echo "86584a54def73570558f66f5111cc53dfed56689637ae32c1201205d494f54fb  helm-v4.3.0-linux-amd64.tar.gz" | sha256sum -c -
tar -xzf helm-v4.3.0-linux-amd64.tar.gz linux-amd64/helm
sudo install -m 0755 linux-amd64/helm /usr/local/bin/helm

# macOS, Apple M-series:
cd /tmp
curl -fsSLO https://get.helm.sh/helm-v4.3.0-darwin-arm64.tar.gz
echo "d3870437e1e95b67f8edbde964156c84a26503f560821d40c542441658934fba  helm-v4.3.0-darwin-arm64.tar.gz" | shasum -a 256 -c -
tar -xzf helm-v4.3.0-darwin-arm64.tar.gz darwin-arm64/helm
sudo install -m 0755 darwin-arm64/helm /usr/local/bin/helm
```

Copy `examples/chart/`, fill the blanks in `values.yaml`, and **render before you install,
every time**:

```bash
cp -r ~/inf345-week10-lab/examples/chart ./chart
helm lint ./chart
helm template notes ./chart | less          # the YAML Helm WOULD send - nothing is installed
```

When the output matches your `k8s/`, you may point `path:` at `chart`; the marker accepts it.
Change a setting in `values.yaml`, render again, and watch `checksum/config` change: a new
Pod template, so a rollout by itself (Week 9's "checksum annotation").

**2. Break the chart on purpose.** In `templates/configmap.yaml`, change `nindent 2` to
`nindent 4` and render again. The error names a line of a file that is not on your disk;
`--debug` shows the YAML Helm produced.

**3. A production overlay with Kustomize** (read the `kustomization.yaml` trap in Common
failures first):

```bash
cp ~/inf345-week10-lab/examples/kustomize/k8s-kustomization.yaml k8s/kustomization.yaml
mkdir -p overlays/prod && cp ~/inf345-week10-lab/examples/kustomize/overlays/prod/kustomization.yaml overlays/prod/
kubectl kustomize overlays/prod
```

**4. Let CI propose the deploy.** `examples/bump.yml` opens a pull request that changes the
tag after each publish. A person still merges it; its comments explain why.

**5. See why secrets cannot live in Git** (uses this lab repository, not yours):

```bash
kubectl create namespace secret-demo
kubectl -n secret-demo create secret generic demo-secret --from-literal=PASSWORD=the-real-one
kubectl apply -f ~/inf345-week10-lab/secret-demo/application.yaml
kubectl -n secret-demo get secret demo-secret -o jsonpath='{.data.PASSWORD}' | base64 -d; echo
kubectl -n secret-demo patch secret demo-secret --type merge -p '{"stringData":{"PASSWORD":"the-real-one"}}'
sleep 20; kubectl -n secret-demo get secret demo-secret -o jsonpath='{.data.PASSWORD}' | base64 -d; echo
```

Clean up with `kubectl -n argocd delete application secret-demo` and
`kubectl delete namespace secret-demo`. Then read about **Sealed Secrets** and the
**External Secrets Operator**.

</details>
