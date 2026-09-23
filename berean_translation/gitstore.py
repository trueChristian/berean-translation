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
from .common import ContractError

MANAGED = ('state', 'content', 'index.json', 'STATUS.md')


class GitStore:
    def __init__(self, root: Path, publish: bool = False):
        self.root, self.publish = root, publish

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(['git','-C',str(self.root),*args], check=check, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def checkpoint(self, message: str) -> None:
        if not self.publish:
            return
        branch = self.git('branch','--show-current').stdout.strip()
        if branch != 'main':
            raise ContractError('Production checkpoints may write only the checked-out main branch')
        paths = [p for p in MANAGED if (self.root/p).exists() or self.git('ls-files','--',p).stdout.strip()]
        if not paths:
            return
        self.git('add','-A','--',*paths)
        if not self.git('diff','--cached','--name-only').stdout.strip():
            return
        self.git('-c','user.name=github-actions[bot]', '-c',
                 'user.email=41898282+github-actions[bot]@users.noreply.github.com',
                 'commit','-m',message)
        own = set(self.git('diff-tree','--no-commit-id','--name-only','-r','HEAD').stdout.splitlines())
        parent = self.git('rev-parse','HEAD^').stdout.strip()
        for _ in range(4):
            pushed = self.git('push','origin','HEAD:refs/heads/main', check=False)
            if pushed.returncode == 0:
                return
            self.git('fetch','origin','main')
            remote = self.git('rev-parse','origin/main').stdout.strip()
            if remote == parent:
                raise ContractError('Git push rejected; state is preserved locally. Check Actions contents:write and main rules.')
            if self.git('merge-base','--is-ancestor',parent,remote,check=False).returncode:
                raise ContractError('Remote main history diverged; refusing to overwrite it')
            theirs = set(self.git('diff','--name-only',parent,remote).stdout.splitlines())
            if own & theirs:
                raise ContractError('Concurrent edits touched the same managed files; refusing automatic overwrite')
            result = self.git('rebase','--onto',remote,parent,check=False)
            if result.returncode:
                self.git('rebase','--abort',check=False)
                raise ContractError('Checkpoint rebase conflict; no force push was attempted')
            parent = remote
        raise ContractError('Main kept advancing; no external API side effect may proceed without a durable checkpoint')

    def human_edit_evidence(self, relative: str) -> dict:
        result = self.git('log','-1','--format=%H%n%an%n%ae%n%cI','--',relative).stdout.splitlines()
        if len(result) != 4 or '[bot]' in (result[1] + result[2]).lower():
            raise ContractError('Removing the notice must be committed by a human repository collaborator')
        return dict(zip(('commit','author','email','time'),result))
