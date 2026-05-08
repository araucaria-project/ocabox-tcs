# Production deploy — services01.oca.lan

Single host serves the jk15 BESO guider, its thumbnail HTTP server, and
the Angular guider UI on a single port. Operator URL:

    http://services01.oca.lan:8090/

- `/`         → Angular guider UI (`ocabox-guider-ui` build output)
- `/thumbs/`  → `/storage/poweruser/thumbs/guider/<instance>/<pipeline>/<seq>.jpg`
- `/healthz`  → server health + roots existence JSON

Port 8090 (not 8080) — the host's 8080 is taken by the long-running
`model-stars-presence` uvicorn service.

## Layout

    /home/poweruser/src/
      ocabox-tcs/                 (this repo, branch feat/guider_svc)
      ocabox-guider-ui/           (Angular dashboard, branch master)

Private deps (`araucaria-project/{ocabox,ocabox-common,pyaraucaria,ctc}`)
are pulled by Poetry from GitHub via the URLs declared in
`pyproject.toml` — they don't get a sibling checkout. For Git/Poetry
to read them, services01 has a stored deploy PAT (see "Credentials").

## systemd

    /etc/systemd/system/oca_guider_jk15.service
        → /home/poweruser/src/ocabox-tcs/deploy/systemd/oca_guider_jk15.service
                                                       (symlink target)

The unit file lives in this repo, mirroring the convention of the
existing `oca_flat_overwatch.service` / `oca_weather_overwatch.service`
on the same host. `systemctl daemon-reload` after editing the source.

## Credentials

Poetry resolves the private `ocabox*` deps by HTTPS — services01 has a
read-only deploy PAT stored once via Git's credential helper:

    ssh poweruser@services01.oca.lan
    git config --global credential.helper store
    echo 'https://oca-deployment:ghp_xxx@github.com' >> ~/.git-credentials
    chmod 600 ~/.git-credentials

Both Git CLI and Poetry's git-fetcher pick this up automatically.
Don't commit `.git-credentials`. Rotate the PAT through the same file.

## First-time deploy

    # 1. Toolchain (Node 22 in user-local dir, no sudo needed for build)
    ssh poweruser@services01.oca.lan \
      'mkdir -p ~/local && cd ~/local && \
       NODE_VER=v22.14.0 && \
       curl -fsSL "https://nodejs.org/dist/${NODE_VER}/node-${NODE_VER}-linux-x64.tar.xz" \
         | tar -xJ && \
       ln -sfn node-${NODE_VER}-linux-x64 node'

    # 2. Repos (after credentials are stored — see above)
    ssh poweruser@services01.oca.lan '
        cd ~/src && \
        git clone https://github.com/araucaria-project/ocabox-tcs.git && \
        cd ocabox-tcs && git checkout feat/guider_svc && cd .. && \
        git clone https://github.com/araucaria-project/ocabox-guider-ui.git'

    # 3. Venv on /storage (10 GB / partition would otherwise fill)
    ssh poweruser@services01.oca.lan '
        mkdir -p /storage/poweruser/venvs/ocabox-tcs && \
        ln -sfn /storage/poweruser/venvs/ocabox-tcs ~/src/ocabox-tcs/.venv && \
        python3.12 -m venv ~/src/ocabox-tcs/.venv'

    # 4. Python deps — Poetry pulls ocabox + transitives from GitHub
    ssh poweruser@services01.oca.lan '
        cd ~/src/ocabox-tcs && \
        ~/.local/bin/poetry install --extras "cli guider oca"'

    # 5. Build the UI
    ssh poweruser@services01.oca.lan '
        export PATH=$HOME/local/node/bin:$PATH && \
        cd ~/src/ocabox-guider-ui && \
        npm ci && npx ng build --configuration production'

    # 6. systemd
    ssh poweruser@services01.oca.lan '
        sudo ln -sf /home/poweruser/src/ocabox-tcs/deploy/systemd/oca_guider_jk15.service \
                    /etc/systemd/system/oca_guider_jk15.service && \
        sudo systemctl daemon-reload && \
        sudo systemctl enable --now oca_guider_jk15'

## Updating

After upstream changes are pushed:

    ssh poweruser@services01.oca.lan '
        cd ~/src/ocabox-tcs   && git pull && \
        ~/.local/bin/poetry install --extras "cli guider oca" --sync && \
        cd ~/src/ocabox-guider-ui && git pull && \
        export PATH=$HOME/local/node/bin:$PATH && \
        npm ci && npx ng build --configuration production && \
        rm -rf /storage/poweruser/node_modules/ocabox-guider-ui && \
        mv node_modules /storage/poweruser/node_modules/ocabox-guider-ui && \
        ln -sfn /storage/poweruser/node_modules/ocabox-guider-ui node_modules && \
        sudo systemctl restart oca_guider_jk15'

`npm ci` (and `npm install`) **deletes** the `node_modules` symlink and
recreates it as a real directory on `/`, eating ~430 MB of root disk.
The three-line dance after the build moves it back onto `/storage` and
re-symlinks. If you skip it, root partition fills within a few updates.

If `pyproject.toml` changed (new/updated dep), run `poetry lock` first
on services01 — the lockfile committed in the repo may be ahead of /
behind the deploy box's local state.

## Disk

`/` is small (10 GB). Heavy items are kept on `/storage` (98 GB) via
symlinks created during deploy:

    ~/src/ocabox-tcs/.venv             → /storage/poweruser/venvs/ocabox-tcs
    ~/src/ocabox-guider-ui/node_modules → /storage/poweruser/node_modules/ocabox-guider-ui
    ~/.cache/pypoetry                  → /storage/poweruser/cache/pypoetry
    ~/.cache/pip                       → /storage/poweruser/cache/pip

Thumbnails go directly to `/storage/poweruser/thumbs/guider/` (no
symlink — `/tmp` is wiped on reboot, the absolute path survives).

Bulk astronomy data (FITS, etc.) lives on `/data/fits` (NFS, 14 TB)
or `/data/misc` (NFS, 1 TB).

## Verifying

    curl -sS http://services01.oca.lan:8090/healthz
    sudo systemctl status oca_guider_jk15
    sudo journalctl -u oca_guider_jk15 -f

From a developer mac with NATS configured for the observatory LAN:

    poetry run tcsctl --detailed
