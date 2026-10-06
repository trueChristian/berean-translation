"""Finite source-paired names, original spans, and strict citation boundaries."""
import hashlib
import json
import unittest
from collections import Counter

from berean_translation.common import ContractError
from berean_translation.html import Fragment, protected_reference_numbers, validate_translation
from berean_translation.reference_notation import reference_mentions
from berean_translation.scripture_citations import quotation_reference_mentions
from berean_translation.scripture_evidence import policy
from support import REPO_ROOT


class StructuralQuoteAliasTests(unittest.TestCase):
    def test_new_frozen_prompt_describes_language_scoped_reviewed_aliases(self):
        contract = policy(REPO_ROOT)
        self.assertEqual(contract['selection_normalization_version'], '2')
        self.assertIn('an existing reviewed alias for the target language', contract['prompt_addendum'])

    def test_exact_source_paired_artifact_provenance_and_full_general_gates(self):
        fixture = json.loads((REPO_ROOT/'tests/fixtures/structural_quote_aliases.json').read_text())
        for case in fixture['cases']:
            with self.subTest(task=case['id']):
                archive = {}
                for key, proof in case['provenance'].items():
                    raw = (REPO_ROOT/proof['path']).read_bytes()
                    self.assertEqual(hashlib.sha256(raw).hexdigest(), proof['sha256'])
                    self.assertEqual(hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest(), proof['git_blob_sha'])
                    archive[key] = json.loads(raw)
                task, source, result = archive['task'], archive['source'], archive['result']['result']
                self.assertEqual(task['status'], 'not_ready')
                self.assertEqual(task['review_attempts'], 0)
                candidate = {key: result[key] for key in ('html','title','subtitle','section')}
                validate_translation(source, candidate, language=case['language'])
                fragment = Fragment(candidate['html'], task['article_id'])
                for quote in archive['evidence']['quotes']:
                    text = ''.join(fragment.text_by_block[quote['block']])
                    mentions = quotation_reference_mentions(archive['evidence'], text)
                    self.assertEqual(Counter(identity for _,_,identity in mentions)[quote['reference']], 1)

    def test_verified_aliases_and_native_names_preserve_original_spans(self):
        for tag, book, native, alias, reference in (
            ('it',58,'Ebrei','Eb','Hebrews 1:10-12'),
            ('sv',58,'Hebrews','Hebreerbrevet','Hebrews 1:10-12'),
            ('af',51,'Kolossense','Kol','Colossians 3:20'),
            ('sv',51,'Colossians','Kol','Colossians 3:20'),
            ('nb',51,'Kolossenserne','Kol','Colossians 3:20'),
            ('zh-Hans',58,'希伯来书','希伯来书','Hebrews 1:10-12'),
        ):
            evidence = self.evidence(tag,book,native)
            numeric = reference.split(' ')[-1]
            text = f'— {alias} {numeric}.'
            with self.subTest(tag=tag):
                mentions = quotation_reference_mentions(evidence,text)
                self.assertEqual(mentions,[(2,len(text)-1,reference)])
                self.assertEqual(text[mentions[0][0]:mentions[0][1]],f'{alias} {numeric}')

    @staticmethod
    def evidence(tag='it',book=58,native='Ebrei'):
        return {'language_tag':tag,'quotes':[{'book':book,'target_lookup':'test'}],
                'lookups':{'test':{'result':{'data':{'book_name':native}}}}}

    def test_unknown_aliases_and_hidden_prefixes_cannot_establish_identity(self):
        for tag, book, native, alias, key in (
            ('it',58,'Ebrei','Eb','Hebrews 1:10-12'),
            ('sv',58,'Hebrews','Hebreerbrevet','Hebrews 1:10-12'),
            ('af',51,'Kolossense','Kol','Colossians 3:20'),
            ('nb',51,'Kolossenserne','Kol','Colossians 3:20'),
        ):
            for prefix in ('X','X\u0301','X\u200d','4 ','IV ','fourth '):
                text=prefix+alias+' '+key.split(' ')[-1]
                with self.subTest(tag=tag,text=text):
                    self.assertNotIn(key,[value for _,_,value in quotation_reference_mentions(self.evidence(tag,book,native),text)])
        self.assertNotIn('Hebrews 1:10-12',[v for _,_,v in quotation_reference_mentions(self.evidence('sv'), 'Eb 1:10-12')])
        with self.assertRaises(ContractError):
            quotation_reference_mentions(self.evidence(native='Matthew'),'Matthew 1:10-12')

    def test_source_and_german_aliases_keep_wrong_identity_and_counts_protected(self):
        for source,target,language in (('Mar 9:47','מרקוס 9:47','heb'),
                                       ('Luk 21:29-31','לוקס 21:29-31','heb'),
                                       ('Luk 21:29-31','Lk 21:29-31','deu'),
                                       ('Rom 6:13','Röm 6:13','deu'),
                                       ('Mat 18:7','מתי 18:7','heb'),
                                       ('Tit 2:12','טיטוס 2:12','heb'),
                                       ('1Pe 3:4-5','פטרוס א׳ 3:4-5','heb'),
                                       ('2Co 5:7','קורינתים ב׳ 5:7','heb'),
                                       ('2Co 5:7','2 Kor 5:7','deu')):
            self.assertEqual(*protected_reference_numbers(source,target,language=language))
            for bad in (target+'; '+target,target.replace(':','1:'), ''):
                with self.subTest(target=bad):
                    self.assertNotEqual(*protected_reference_numbers(source,bad,language=language))
        self.assertNotEqual(*protected_reference_numbers('Mar 9:47','מתי 9:47',language='heb'))
        self.assertNotEqual(*protected_reference_numbers('1Pe 3:4-5','2 פטרוס 3:4-5',language='heb'))
        self.assertNotEqual(*protected_reference_numbers('2Co 5:7','1. Korinther 5:7',language='deu'))

    def test_native_aliases_never_rewrite_original_text(self):
        text='— 希伯来书 1:10-12. Hebrew prose.'
        before=text
        result=quotation_reference_mentions(self.evidence('zh-Hans',58,'希伯来书'),text)
        self.assertEqual(text,before)
        self.assertEqual(text[result[0][0]:result[0][1]],'希伯来书 1:10-12')


if __name__ == '__main__':
    unittest.main()
