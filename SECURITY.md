# Security

## Reporting a vulnerability

Please **don't** open a public issue for a security problem.

E-mail **PlunderHub@proton.me** with:

- what the problem is and what someone could do with it
- how to reproduce it (PlunderHub version, distro, desktop)

You'll get an answer within a few days. Once it's fixed, the fix is released and the problem is described in the release notes - with credit to you, if you'd like.

## What to look at

PlunderHub runs as your user. Two parts do more, and are the most interesting to check:

- **Shipwright and Ship inspection** install packages as root. The password is asked once through polkit (`pkexec`); a small helper (`plunderhub/core/root_helper.py`) then runs only package-manager commands and a fixed list of system fixes, and only for PlunderHub's own processes.
- **Updates** download a newer release's source from this repository's GitHub releases and install it the way PlunderHub was installed - its `install.sh`, or `pacman -U` for the Arch package - through `pkexec` (`plunderhub/core/self_update.py`). A git checkout just runs `git pull`.

## Supported versions

Only the latest release gets fixes.
