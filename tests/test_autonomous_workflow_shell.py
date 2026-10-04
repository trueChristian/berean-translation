"""Execute the actual scheduled collector shell with only external boundaries faked."""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
import yaml
from berean_translation.common import canonical, read_json
from support import REPO_ROOT, setup

BOOTSTRAP = r'''
import functools
import sys
from pathlib import Path
from support import FixtureSource, FakeProvider, MemoryGit
from berean_translation import cli
from berean_translation.collector import collect_window

def forbid_network(event, args):
    if event in ('socket.connect', 'socket.getaddrinfo', 'socket.sendto'):
        raise AssertionError('Network forbidden by actual workflow shell fixture')
sys.addaudithook(forbid_network)
source = None

def source_client(config):
    global source
    source = FixtureSource(config.root)
    source.articles = source.articles[:1]
    source.rebuild()
    return source.client

provider = FakeProvider()
clock = [0]
def sleep(seconds):
    clock[0] += seconds
    provider.complete_all()

cli.SourceClient = source_client
cli.OpenAIProvider = lambda: provider
cli.GitStore = lambda *args, **kwargs: MemoryGit()
cli.collect_window = functools.partial(collect_window, monotonic=lambda: clock[0], sleep=sleep)
'''


class AutonomousWorkflowShellTests(unittest.TestCase):
    def test_actual_collector_shell_discovers_all_languages_and_finishes_without_manual_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'checkout'; root.mkdir()
            setup(root)
            runtime = read_json(root/'config/runtime.json')
            runtime['automatic_new_translation'] = True
            runtime['autonomous_translation'].update(enabled=True, page_size=20, max_active_tasks=50)
            (root/'config/runtime.json').write_bytes(canonical(runtime))
            stub = Path(directory)/'stub'; stub.mkdir()
            (stub/'sitecustomize.py').write_text(textwrap.dedent(BOOTSTRAP))
            binary = root/'.venv/bin/python'; binary.parent.mkdir(parents=True)
            binary.symlink_to(sys.executable)
            document = yaml.load((REPO_ROOT/'.github/workflows/ai-worker.yml').read_text(), Loader=yaml.BaseLoader)
            step = next(step for step in document['jobs']['worker']['steps']
                        if step.get('name') == 'Collect results or perform the requested maintenance')
            self.assertIn('--publish --wait-seconds 600 --poll-seconds 60', step['run'])
            self.assertEqual(document['concurrency'], {'group':'berean-translation-state-writer', 'cancel-in-progress':'false'})
            environment = {'PATH':os.environ.get('PATH', os.defpath),
                'PYTHONPATH':os.pathsep.join((str(stub), str(REPO_ROOT), str(REPO_ROOT/'tests'))),
                'PYTHONNOUSERSITE':'1', 'PYTHONDONTWRITEBYTECODE':'1', 'OPERATION':'collect',
                'OPENAI_API_KEY':'offline-fixture-not-a-real-credential',
                'GITHUB_REF':'refs/heads/main', 'GITHUB_EVENT_NAME':'schedule'}
            result = subprocess.run(['bash','-c',step['run']], cwd=root, env=environment,
                                    capture_output=True, text=True, timeout=180)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            index = read_json(root/'index.json')
            self.assertEqual(len(index['articles']), 20)
            self.assertEqual({a['language'] for a in index['articles']}, set(read_json(root/'config/languages.json')))
            self.assertTrue(list((root/'state/automatic-settlements').glob('*.json')))
            self.assertFalse(list((root/'state/queue').glob('gh-*.json')))


if __name__ == '__main__': unittest.main()
