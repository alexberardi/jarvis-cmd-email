# jarvis-cmd-email

## Loop (agents)

**Email (Gmail & IMAP)**: Full email management — list, read, search, send, reply, archive, trash, and star. A Jarvis package (agent, command) that nodes install through Pantry.

**Verify** (what CI runs: `.github/workflows/ci.yml` calls the toolkit's reusable `package-ci.yml`):

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install "jarvis-command-sdk @ git+https://github.com/alexberardi/jarvis-command-sdk@main" \
  "jarvis-developer-toolkit @ git+https://github.com/alexberardi/jarvis-developer-toolkit@main" pytest
jdt test . --install-deps
python -m pytest tests
```

**Merging.** An agent may squash-merge **its own** PR into `main` once CI has passed, then check main's CI after the merge. **Hold for the user's review** instead, with a note saying why, when the PR:

- changes `jarvis_package.yaml` at all (its `version`, `secrets`, `packages`, `apt_packages` and `post_install` are what reach users' nodes);
- touches `*_scripts/**` or `*_shared/**` (OAuth and token code lives there);
- touches `.github/**`;
- deletes, renames or weakens a test;
- adds a secret (`JarvisSecret`) or a new outbound host, or is marked "needs review" by its author.

**Releases are the user's.** Tagging `v<version>` and submitting to Pantry are always done by the user: never push a tag or submit. Never merge someone else's PR, never force-push to `main`.
