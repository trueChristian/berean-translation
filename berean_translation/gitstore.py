"""Durable checkpoints with conservative, non-force Git publication.

A worker owns the state paths. Enqueuers add unique immutable queue files and may
advance main concurrently; only disjoint remote edits can be rebased automatically.
"""
from __future__ import annotations
import json
import os
import re
import subprocess
from pathlib import Path
from .common import ContractError, loads, digest, json_hash, read_regular_bytes

MANAGED = ('state', 'content', 'index.json', 'STATUS.md', 'RECOVERY.json')
BOT_IDENTITY = ('-c', 'user.name=github-actions[bot]', '-c',
                'user.email=41898282+github-actions[bot]@users.noreply.github.com')


class GitStore:
    def __init__(self, root: Path, publish: bool = False):
        self.root, self.publish = root, publish

    def git(self, *args: str, check: bool = True, input: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(['git','-C',str(self.root),*args], check=check, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, input=input)

    def checkpoint(self, message: str) -> None:
        if not self.publish:
            return
        branch = self.git('branch','--show-current').stdout.strip()
        if branch != 'main':
            raise ContractError('Production checkpoints may write only the checked-out main branch')
        from .state import State
        state = State(self.root)
        publications = {pub[field]: (pub, field) for record in state.records()
                        if (pub := record.get('published'))
                        for field in ('html_path', 'metadata_path')}
        accepted = set(publications)
        paths = [p for p in MANAGED if p != 'content' and
                 ((self.root/p).exists() or self.git('ls-files','--',p).stdout.strip())]
        staged = set(self.git('diff', '--cached', '--name-only', '-z').stdout.split('\0')) - {''}
        def allowed(path):
            return path in accepted or any(path == p or path.startswith(p + '/')
                                           for p in MANAGED if p != 'content')
        if any(not allowed(path) for path in staged):
            raise ContractError('An isolated or unmanaged file is already staged; retain staging for owner attention')
        if paths:
            self.git('add', '-A', '--pathspec-from-file=-', '--pathspec-file-nul',
                     input=''.join(':(literal)' + path + '\0' for path in paths))
        # A pathspec may recurse if a file becomes a directory. Stage exact,
        # hash-verified regular-file blobs instead, only for changed accepted
        # content. Unknown files cannot widen this allowlist during a checkpoint.
        dirty = set(self.git('ls-files', '--modified', '--deleted', '-z', '--', 'content').stdout.split('\0')) - {''}
        dirty |= staged & accepted
        dirty |= set(self.git('ls-files', '--others', '--exclude-standard', '-z', '--', 'content').stdout.split('\0')) - {''}
        tracked = set(self.git('ls-files', '-z', '--', 'content').stdout.split('\0')) - {''}
        updates = []
        for path in sorted(accepted):
            publication, field = publications[path]
            if path not in dirty:
                if path not in tracked and not os.path.lexists(self.root/path) and not publication.get('edit_issue'):
                    raise ContractError('Accepted publication vanished before its checkpoint')
                continue
            if publication.get('edit_issue'):
                if path in staged:
                    raise ContractError('An isolated accepted working file is already staged; preserve staging for owner attention')
                continue  # Retain the committed accepted blob; never add a technical replacement.
            data = read_regular_bytes(state.path(path))
            actual = digest(data) if field == 'html_path' else json_hash(loads(data))
            expected = publication['html_sha256' if field == 'html_path' else 'metadata_sha256']
            if actual != expected:
                raise ContractError('Accepted publication changed during its checkpoint')
            sha = self.git('hash-object', '-w', '--stdin', input=data.decode('utf-8')).stdout.strip()
            updates.append('100644 ' + sha + '\t' + path + '\0')
        if updates:
            self.git('update-index', '-z', '--index-info', input=''.join(updates))
        if not self.git('diff','--cached','--name-only').stdout.strip():
            return
        self.git(*BOT_IDENTITY, 'commit', '-m', message)
        own = set(self.git('diff-tree','--no-commit-id','--name-only','-r','HEAD').stdout.splitlines())
        parent = self.git('rev-parse','HEAD^').stdout.strip()
        for _ in range(4):
            pushed = self.git('push','origin','HEAD:refs/heads/main', check=False)
            if pushed.returncode == 0:
                return
            self.git('fetch','origin','main')
            remote = self.git('rev-parse','origin/main').stdout.strip()
            head = self.git('rev-parse','HEAD').stdout.strip()
            if remote == head:
                return  # The push succeeded but its acknowledgement was lost.
            if self.git('merge-base','--is-ancestor',head,remote,check=False).returncode == 0:
                self.git('merge','--ff-only','origin/main')
                return
            if remote == parent:
                raise ContractError('Git push rejected; state is preserved locally. Check Actions contents:write and main rules.')
            if self.git('merge-base','--is-ancestor',parent,remote,check=False).returncode:
                raise ContractError('Remote main history diverged; refusing to overwrite it')
            theirs = set(self.git('diff','--name-only',parent,remote).stdout.splitlines())
            if own & theirs:
                raise ContractError('Concurrent edits touched the same managed files; refusing automatic overwrite')
            if any(p.startswith(('berean_translation/','config/','prompts/','.github/')) or
                   p in ('requirements.txt','pyproject.toml') for p in theirs):
                raise ContractError('Runtime code/configuration changed concurrently; resume on a fresh checkout')
            result = self.git(*BOT_IDENTITY, 'rebase', '--onto', remote, parent, check=False)
            if result.returncode:
                self.git('rebase','--abort',check=False)
                raise ContractError('Checkpoint rebase conflict; no force push was attempted')
            parent = remote
        raise ContractError('Main kept advancing; no external API side effect may proceed without a durable checkpoint')

    def require_published_checkpoint(self) -> None:
        """Read-only confirmation for terminal abort retries with no new diff.

        A failed push can leave an already-created local commit. Ordinary
        checkpoint() has no staged change to retry in that case; never report a
        terminal abort as durable unless origin contains that exact commit.
        """
        if not self.publish:
            return
        self.git('fetch', 'origin', 'main')
        head = self.git('rev-parse', 'HEAD').stdout.strip()
        remote = self.git('rev-parse', 'origin/main').stdout.strip()
        if self.git('merge-base', '--is-ancestor', head, remote, check=False).returncode:
            raise ContractError('Terminal abort checkpoint is still local, not published on origin/main. '
                                'Run cancel for the same campaign from a fresh main checkout; '
                                'retain the failed checkout for audit. No allocation was released.')

    def human_edit_evidence(self, relative: str) -> dict:
        if self.git('status', '--porcelain', '--untracked-files=all', '--', relative).stdout.strip():
            raise ContractError('Human review edits must be committed before synchronization')
        result = self.git('log','-1','--format=%H%n%an%n%ae%n%cI','--',relative).stdout.splitlines()
        if len(result) != 4 or '[bot]' in (result[1] + result[2]).lower():
            raise ContractError('Removing the notice must be committed by a human repository collaborator')
        return dict(zip(('commit','author','email','time'),result))

    def publication_at_commit(self, publication: dict, commit: str) -> dict:
        if not re.fullmatch(r'[0-9a-f]{40}', commit):
            raise ContractError('Invalid human publication commit')
        try:
            return {'html':self.git('show', f'{commit}:{publication["html_path"]}').stdout,
                    'metadata':loads(self.git('show', f'{commit}:{publication["metadata_path"]}').stdout)}
        except subprocess.CalledProcessError as exc:
            raise ContractError('Recorded human publication is unavailable in Git history') from exc

    def matching_publication(self, publication: dict, history=()) -> dict:
        result = {}
        for field, key, expected in (('html_path','html',publication['html_sha256']),
                                      ('metadata_path','metadata',publication['metadata_sha256'])):
            commits = self.git('log', '--format=%H', '--', publication[field]).stdout.splitlines()
            for commit in commits:
                value = self.git('show', f'{commit}:{publication[field]}', check=False)
                if value.returncode:
                    continue
                try:
                    payload = value.stdout if key == 'html' else loads(value.stdout)
                    actual = digest(payload) if key == 'html' else json_hash(payload)
                except ContractError:
                    continue
                if actual == expected:
                    result[key] = payload
                    break
            if key not in result:
                raise ContractError('Hash-verified accepted publication is unavailable in Git history')
        return result
