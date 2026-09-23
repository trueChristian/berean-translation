"""Real local Git repositories exercise non-force checkpoint conflict handling."""
from __future__ import annotations
import subprocess
import tempfile
import unittest
from pathlib import Path
from berean_translation.common import ContractError
from berean_translation.gitstore import GitStore


class GitStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root/'remote.git'; self.worker = self.root/'worker'; self.other = self.root/'other'
        self.run_git(self.root,'init','--bare',str(self.remote))
        self.run_git(self.root,'init','-b','main',str(self.worker))
        self.configure(self.worker)
        (self.worker/'README.md').write_text('fixture')
        self.commit(self.worker,'initial')
        self.run_git(self.worker,'remote','add','origin',str(self.remote))
        self.run_git(self.worker,'push','-u','origin','main')
        self.run_git(self.remote,'symbolic-ref','HEAD','refs/heads/main')
        self.run_git(self.root,'clone',str(self.remote),str(self.other)); self.configure(self.other)
        self.store = GitStore(self.worker,publish=True)

    def run_git(self,root,*args):
        return subprocess.run(['git','-C',str(root),*args],check=True,capture_output=True,text=True).stdout.strip()

    def configure(self,root):
        self.run_git(root,'config','user.name','Fixture reviewer')
        self.run_git(root,'config','user.email','reviewer@example.test')

    def commit(self,root,message):
        self.run_git(root,'add','-A'); self.run_git(root,'commit','-m',message)

    def test_first_checkpoint_handles_absent_content_directory(self):
        (self.worker/'state').mkdir(); (self.worker/'state/source.json').write_text('{}')
        self.store.checkpoint('test checkpoint')
        remote = self.run_git(self.remote,'show','main:state/source.json')
        self.assertEqual(remote,'{}')

    def test_concurrent_queue_addition_is_preserved(self):
        (self.other/'state/queue').mkdir(parents=True)
        (self.other/'state/queue/request.json').write_text('{}')
        self.commit(self.other,'enqueue'); self.run_git(self.other,'push')
        (self.worker/'state').mkdir(); (self.worker/'state/source.json').write_text('{}')
        self.store.checkpoint('worker state')
        files = self.run_git(self.remote,'ls-tree','-r','--name-only','main').splitlines()
        self.assertIn('state/queue/request.json',files)
        self.assertIn('state/source.json',files)

    def test_concurrent_checkpoint_without_runner_git_identity(self):
        # Hosted checkout does not configure user.name/email. Rebasing must use
        # the same explicit bot identity as the original checkpoint commit.
        self.run_git(self.worker, 'config', '--unset', 'user.name')
        self.run_git(self.worker, 'config', '--unset', 'user.email')
        self.run_git(self.worker, 'config', 'user.useConfigOnly', 'true')
        self.test_concurrent_queue_addition_is_preserved()
        committer = self.run_git(self.remote, 'log', '-1', '--format=%cn <%ce>', 'main')
        self.assertEqual(committer, 'github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>')

    def test_same_file_conflict_is_never_silently_rebased(self):
        (self.other/'state').mkdir(); (self.other/'state/source.json').write_text('{"human":true}')
        self.commit(self.other,'human edit'); self.run_git(self.other,'push')
        (self.worker/'state').mkdir(); (self.worker/'state/source.json').write_text('{"bot":true}')
        with self.assertRaisesRegex(ContractError,'same managed files'):
            self.store.checkpoint('worker conflict')
        self.assertEqual(self.run_git(self.remote,'show','main:state/source.json'),'{"human":true}')

    def test_runtime_code_change_requires_a_fresh_worker(self):
        (self.other/'config').mkdir(); (self.other/'config/runtime.json').write_text('{}')
        self.commit(self.other,'new configuration'); self.run_git(self.other,'push')
        (self.worker/'state').mkdir(); (self.worker/'state/source.json').write_text('{}')
        with self.assertRaisesRegex(ContractError,'fresh checkout'):
            self.store.checkpoint('old worker state')

    def test_non_main_checkpoint_is_refused(self):
        self.run_git(self.worker,'switch','-c','feature')
        (self.worker/'state').mkdir(); (self.worker/'state/source.json').write_text('{}')
        with self.assertRaises(ContractError): self.store.checkpoint('must not publish')

    def test_human_review_attribution_comes_from_git_history(self):
        value = self.store.human_edit_evidence('README.md')
        self.assertEqual(value['author'],'Fixture reviewer')
        self.assertEqual(len(value['commit']),40)


if __name__ == '__main__': unittest.main()
