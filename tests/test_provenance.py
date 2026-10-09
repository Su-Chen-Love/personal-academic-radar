import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from academic_radar.storage import upgrade_database,connect,utc_now
from academic_radar.provenance import verify_provenance
from academic_radar.cloud_sync import public_source_url

class ProvenanceTests(unittest.TestCase):
 def seed(self,path):
  upgrade_database(path);db=connect(path);stamp=utc_now()
  with db:db.execute("INSERT INTO papers(identity,doi,title,abstract,first_seen,updated_at) VALUES('doi:1','1','Research','Original full abstract',?,?)",(stamp,stamp))
  db.close()
 def test_exact_verified_text_sets_real_provenance_and_is_idempotent(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'papers.sqlite3';self.seed(p)
   found={'abstract':'Original full abstract','source_name':'crossref','source_url':'https://api.crossref.org/works/1','evidence_type':'crossref_metadata'}
   with patch('academic_radar.provenance.lookup_crossref',return_value=found),patch('academic_radar.provenance.lookup_openalex') as secondary:
    result=verify_provenance(p,{});self.assertEqual(result['verified'],1);secondary.assert_not_called()
    self.assertEqual(verify_provenance(p,{})['checked'],0)
   db=connect(p);row=db.execute('SELECT * FROM papers').fetchone();self.assertEqual(row['abstract'],'Original full abstract');self.assertTrue(row['abstract_retrieved_at']);self.assertEqual(row['needs_rescreen'],0);db.close()
 def test_different_api_abstract_preserves_original_and_missing_provenance(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'papers.sqlite3';self.seed(p)
   with patch('academic_radar.provenance.lookup_crossref',return_value={'abstract':'Different text'}),patch('academic_radar.provenance.lookup_openalex',return_value=None):
    result=verify_provenance(p,{});self.assertEqual(result['verified'],0)
   db=connect(p);r=db.execute('SELECT * FROM papers').fetchone();self.assertEqual(r['abstract'],'Original full abstract');self.assertIsNone(r['abstract_retrieved_at']);db.close()
 def test_both_providers_in_cooldown_defer_without_recording_fake_attempts(self):
  from academic_radar.enrichment import ProviderTemporarilyUnavailable
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'papers.sqlite3';self.seed(p)
   with patch('academic_radar.provenance.lookup_crossref',side_effect=ProviderTemporarilyUnavailable()),patch('academic_radar.provenance.lookup_openalex',side_effect=ProviderTemporarilyUnavailable()):
    result=verify_provenance(p,{})
   self.assertEqual(result['status'],'partial');self.assertEqual(result['checked'],0);self.assertEqual(result['deferred'],1)
   db=connect(p);self.assertEqual(db.execute('SELECT COUNT(*) FROM abstract_attempts').fetchone()[0],0);db.close()
 def test_public_provenance_links_drop_contact_and_credential_query(self):
  self.assertEqual(public_source_url('https://api.crossref.org/works/1?mailto=private@example.test&key=secret'),'https://api.crossref.org/works/1')
  self.assertIsNone(public_source_url('file:///private/library/paper.pdf'))
  self.assertIsNone(public_source_url('https://user:secret@example.test/article'))

if __name__=='__main__':unittest.main()
