import importlib.util, io, json, sqlite3, sys, tempfile, unittest, urllib.error
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).parents[1] / "scripts" / "paper_monitor.py"
spec = importlib.util.spec_from_file_location("paper_monitor", SCRIPT)
pm = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pm
spec.loader.exec_module(pm)

class MonitorTests(unittest.TestCase):
    def test_doi_normalization_and_identity(self):
        self.assertEqual(pm.normalize_doi("https://doi.org/10.1145/ABC. "), "10.1145/abc")
        self.assertEqual(pm.identity("10.1/X", "A"), pm.identity("doi:10.1/x", "B"))
        self.assertEqual(pm.identity("", "Human–AI  Systems"), pm.identity("", "Human AI Systems"))

    def test_clean_structured_abstract(self):
        raw = "<jats:p>Preference &amp; routing</jats:p>\n  test"
        self.assertEqual(pm.clean_text(raw), "Preference & routing test")

    def test_crossref_parsing_and_chi_filter(self):
        fixture = json.loads((Path(__file__).parent/"fixtures"/"crossref.json").read_text())
        cfg={"collection":{},"user_agent":"test"}
        with patch.object(pm,"request_json",return_value=fixture):
            papers=pm.crossref_collect({"name":"CHI","type":"crossref-query","query_container":"CHI Conference"},cfg,"2026-01-01")
        self.assertEqual(len(papers),1)
        self.assertEqual(papers[0].doi,"10.1145/3706598.3710001")
        self.assertIn("Human-AI",papers[0].title)

    def test_crossref_cursor_pagination_and_deduplication(self):
        def page(doi,title,next_cursor):
            return {"message":{"items":[{"DOI":doi,"title":[title],"container-title":["Venue"]}],
                               "next-cursor":next_cursor}}
        responses=[page("10.1/a","A","next"),page("10.1/b","B",None)]
        cfg={"collection":{"rows_per_page":1,"max_pages_per_source":3},"user_agent":"test"}
        with patch.object(pm,"request_json",side_effect=responses) as request:
            papers=pm.crossref_collect({"name":"V","type":"crossref","issn":"1234"},cfg,"2026-01-01")
        self.assertEqual([p.doi for p in papers],["10.1/a","10.1/b"])
        self.assertEqual(request.call_count,2)
        self.assertIn("cursor=next",request.call_args_list[1].args[0])

    def test_retry_honors_retry_after_then_succeeds(self):
        error=urllib.error.HTTPError("https://example.test",429,"limited",{"Retry-After":"0"},io.BytesIO())
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): return False
            def read(self): return b'{"ok": true}'
        with patch.object(pm.urllib.request,"urlopen",side_effect=[error,Response()]), patch.object(pm.time,"sleep") as sleep:
            result=pm.request_json("https://example.test",{},1,1,0.1)
        self.assertTrue(result["ok"]); sleep.assert_called_once_with(0.0)

    def test_openalex_survives_crossref_failure_and_marks_degraded(self):
        with tempfile.TemporaryDirectory() as td:
            db=pm.db_open(Path(td)/"x.sqlite3")
            paper=pm.Paper("doi:10.1/x","10.1/x","A","Abstract","V","2026-01-01","u",[],"V / OpenAlex")
            cfg={"lookback_days":1,"collection":{"openalex_fallback":True},"sources":[
                {"name":"V","type":"crossref","issn":"1234","openalex_id":"S1"}]}
            with patch.object(pm,"crossref_collect",side_effect=RuntimeError("down")), \
                 patch.object(pm,"openalex_collect",return_value=[paper]):
                collected,new,failures=pm.collect_into_db(cfg,db,"now","run")
            self.assertEqual((len(collected),len(new)),(1,1))
            self.assertEqual(failures[0]["status"],"degraded")
            health=db.execute("select status,consecutive_failures from source_health where source='V'").fetchone()
            self.assertEqual(tuple(health),("degraded",0))

    def test_normal_agent_export_enriches_before_freezing_single_queue(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/"research-profile.md").write_text("profile",encoding="utf-8")
            config=root/"config.toml"
            config.write_text('state_dir = "."\nprofile_file = "research-profile.md"\n[[sources]]\nname = "A"\ntype = "crossref"\nissn = "1234"\n',encoding="utf-8")
            paper=pm.Paper("doi:10.1/x","10.1/x","A","","V","2026-01-01","u",[],"s",
                           publication_type_raw="journal-article",publication_type_source="crossref")
            def collected(cfg,db,now,run_id):
                pm.upsert(db,paper,now); db.commit(); return [paper],[paper],[]
            def enriched(path,cfg,limit=500):
                db=pm.db_open(path)
                with db: db.execute("UPDATE papers SET abstract='Recovered',needs_rescreen=1")
                db.close(); return {"updated":1,"unresolved":0,"status":"succeeded"}
            with patch.object(pm,"collect_into_db",side_effect=collected), patch.object(pm,"run_enrichment",side_effect=enriched), patch("builtins.print") as output:
                pm.agent_export(config)
            summary=json.loads(output.call_args.args[0]); queue=json.loads(Path(summary["queue_path"]).read_text())
            self.assertEqual(summary["enrichment"]["updated"],1)
            self.assertEqual(queue["papers"][0]["abstract"],"Recovered")
            db=pm.db_open(root/"papers.sqlite3")
            self.assertIsNotNone(db.execute("SELECT 1 FROM run_papers WHERE run_id=? AND role='new'",(summary["run_id"],)).fetchone())
            db.close()

    def test_duplicate_provider_records_share_one_abstract_lookup(self):
        a=pm.Paper("doi:10.1/x","10.1/x","A","","V","2026-01-01","u",[],"crossref")
        b=pm.Paper("doi:10.1/x","10.1/x","A","","V","2026-01-01","u",[],"openalex")
        with patch.object(pm,"openalex_abstract",return_value="Shared abstract") as lookup:
            pm.enrich_missing_abstracts([a,b],{"collection":{"openalex_fallback":True}})
        lookup.assert_called_once(); self.assertEqual((a.abstract,b.abstract),("Shared abstract","Shared abstract"))

    def test_existing_database_abstract_avoids_network_lookup(self):
        with tempfile.TemporaryDirectory() as td:
            db=pm.db_open(Path(td)/"x.sqlite3")
            stored=pm.Paper("doi:10.1/x","10.1/x","A","Stored abstract","V","2026-01-01","u",[],"old")
            pm.upsert(db,stored,"now"); db.commit()
            incoming=pm.Paper("doi:10.1/x","10.1/x","A","","V","2026-01-01","u",[],"new")
            with patch.object(pm,"openalex_abstract") as lookup:
                pm.enrich_missing_abstracts([incoming],{},db)
            lookup.assert_not_called(); self.assertEqual(incoming.abstract,"Stored abstract")

    def test_upsert_is_idempotent_and_enriches_abstract(self):
        with tempfile.TemporaryDirectory() as td:
            db=pm.db_open(Path(td)/"x.sqlite3")
            p=pm.Paper(pm.identity("10.1/x","A"),"10.1/x","A","","V","2026-01-01","u",[],"s1")
            self.assertTrue(pm.upsert(db,p,"t1")); db.commit()
            p.abstract="A longer abstract"; p.source="s2"
            self.assertFalse(pm.upsert(db,p,"t2")); db.commit()
            self.assertEqual(db.execute("select count(*) from papers").fetchone()[0],1)
            self.assertEqual(db.execute("select abstract from papers").fetchone()[0],"A longer abstract")
            self.assertEqual(db.execute("select abstract_source from papers").fetchone()[0],"metadata")
            self.assertEqual(db.execute("select count(*) from observations").fetchone()[0],2)

    def test_agent_export_skips_low_priority_papers_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/"research-profile.md").write_text("profile",encoding="utf-8")
            config=root/"config.toml"
            config.write_text('state_dir = "."\nprofile_file = "research-profile.md"\n[[sources]]\nname = "A"\ntype = "crossref"\nissn = "1234"\n',encoding="utf-8")
            db=pm.db_open(root/"papers.sqlite3")
            pm.upsert(db,pm.Paper("doi:10.1/x","10.1/x","Editorial Board","","V","2026-01-01","u",[],"s"),"now")
            db.commit(); db.close()
            with patch("builtins.print") as output:
                pm.agent_export(config,no_collect=True)
            summary=json.loads(output.call_args.args[0])
            queue=json.loads(Path(summary["queue_path"]).read_text())
            self.assertEqual(queue["papers"],[])

    def test_enriched_abstract_forces_rescreen_and_exports_publication_type(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); profile="profile"; (root/"research-profile.md").write_text(profile,encoding="utf-8")
            config=root/"config.toml"
            config.write_text('state_dir = "."\nprofile_file = "research-profile.md"\n[[sources]]\nname = "A"\ntype = "crossref"\nissn = "1234"\n',encoding="utf-8")
            db=pm.db_open(root/"papers.sqlite3")
            paper=pm.Paper("doi:10.1/x","10.1/x","A","Recovered abstract","V","2026-01-01","u",[],"s",
                           publication_type_raw="journal-article",publication_type_source="crossref")
            pm.upsert(db,paper,"now")
            profile_hash=__import__("hashlib").sha256(profile.encode()).hexdigest()
            db.execute("""INSERT INTO screenings(identity,profile_hash,provider,model,relevant,score,screened_at)
                        VALUES(?,?,'codex-agent','old',0,0.1,'before')""",(paper.identity,profile_hash))
            db.execute("UPDATE papers SET needs_rescreen=1 WHERE identity=?",(paper.identity,))
            db.commit(); db.close()
            with patch("builtins.print") as output: pm.agent_export(config,no_collect=True)
            queue=json.loads(Path(json.loads(output.call_args.args[0])["queue_path"]).read_text())
            self.assertEqual([item["identity"] for item in queue["papers"]],[paper.identity])
            self.assertEqual(queue["papers"][0]["publication_type"],"Journal Article")

    def test_direct_model_provider_paths_are_removed(self):
        self.assertFalse(hasattr(pm,"llm_screen"))
        self.assertFalse(hasattr(pm,"heuristic_screen"))
        self.assertFalse(hasattr(pm,"run"))

    def test_model_json_validation(self):
        out=pm.extract_json('```json\n{"relevant":true,"score":2,"reasons":"x","matched_themes":["a"],"confidence":-1}\n```')
        self.assertEqual(out["score"],1); self.assertEqual(out["confidence"],0)
        with self.assertRaises(ValueError): pm.extract_json('{"relevant":true}')

    def test_enrich_abstracts_updates_stored_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "research-profile.md").write_text("profile", encoding="utf-8")
            config = root / "config.toml"
            config.write_text(
                'state_dir = "."\nprofile_file = "research-profile.md"\n'
                '[[sources]]\nname = "A"\ntype = "crossref"\nissn = "1234"\n',
                encoding="utf-8",
            )
            db = pm.db_open(root / "papers.sqlite3")
            paper=pm.Paper("doi:10.1/x", "10.1/x", "A", "", "V", "", "", [], "s")
            paper.publication_type_raw="journal-article"; paper.publication_type_source="crossref"
            pm.upsert(db,paper,"now")
            db.commit()
            db.close()
            def found(db,paper,client):
                return {"abstract":"Recovered abstract","source_name":"crossref","source_url":"https://api.crossref.org/v1/works/10.1/x",
                        "evidence_type":"crossref_metadata","publication_type_raw":"journal-article","publication_type_source":"crossref"}
            with patch("academic_radar.enrichment.PROVIDERS",[("crossref",found)]), patch("builtins.print"):
                self.assertEqual(pm.enrich_abstracts(config), 0)
            db = pm.db_open(root / "papers.sqlite3")
            row = db.execute("SELECT abstract,abstract_source FROM papers").fetchone()
            self.assertEqual(tuple(row), ("Recovered abstract", "crossref"))
            db.close()

    def test_python39_toml_fallback(self):
        text='state_dir = "~/x"\nflag = true\n[s]\nn = 3\n[[sources]]\nname = "A"\ntype = "crossref"\n'
        out=pm._toml_load_fallback(text)
        self.assertEqual(out["s"]["n"],3); self.assertEqual(out["sources"][0]["name"],"A")

    def test_agent_import_records_semantic_judgment(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/"research-profile.md").write_text("interactive optimization",encoding="utf-8")
            (root/"config.toml").write_text('state_dir = "."\nprofile_file = "research-profile.md"\nrelevance_threshold = 0.62\n[[sources]]\nname = "A"\ntype = "crossref"\nissn = "1234-5678"\n',encoding="utf-8")
            db=pm.db_open(root/"papers.sqlite3")
            p=pm.Paper("doi:10.1/x","10.1/x","Preference-aware routing","Abstract","V","2026-01-01","u",[],"s")
            p.publication_type_raw="journal-article"; p.publication_type_source="crossref"
            pm.upsert(db,p,"now"); db.commit(); db.close()
            profile_hash=__import__("hashlib").sha256(b"interactive optimization").hexdigest()
            with patch("builtins.print") as output:
                pm.agent_export(root/"config.toml",no_collect=True)
            run_id=json.loads(output.call_args.args[0])["run_id"]
            results={"run_id":run_id,"profile_hash":profile_hash,"model":"codex-test","results":[{
              "identity":p.identity,"relevant":True,"score":0.9,"reasons":"Directly studies the core problem",
              "matched_themes":["interactive optimization"],"confidence":0.95}]}
            path=root/"results.json"; path.write_text(json.dumps(results),encoding="utf-8")
            self.assertEqual(pm.agent_import(root/"config.toml",path),0)
            db=pm.db_open(root/"papers.sqlite3")
            row=db.execute("select provider,relevant,score from screenings").fetchone()
            self.assertEqual((row[0],row[1],row[2]),("codex-agent",1,0.9))

    def test_agent_import_rejects_partial_exported_queue(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/"research-profile.md").write_text("profile",encoding="utf-8")
            config=root/"config.toml"
            config.write_text('state_dir = "."\nprofile_file = "research-profile.md"\n[[sources]]\nname = "A"\ntype = "crossref"\nissn = "1234"\n',encoding="utf-8")
            db=pm.db_open(root/"papers.sqlite3")
            paper=pm.Paper("doi:10.1/x","10.1/x","A","Abstract","V","2026-01-01","u",[],"s")
            paper.publication_type_raw="journal-article"; paper.publication_type_source="crossref"
            pm.upsert(db,paper,"now"); db.commit(); db.close()
            with patch("builtins.print") as output:
                self.assertEqual(pm.agent_export(config,no_collect=True),0)
            summary=json.loads(output.call_args.args[0]); queue=json.loads(Path(summary["queue_path"]).read_text())
            results=root/"partial.json"
            results.write_text(json.dumps({"run_id":queue["run_id"],"profile_hash":queue["profile_hash"],
                                           "model":"codex-test","results":[]}),encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"complete queue"):
                pm.agent_import(config,results)

    def test_agent_import_is_atomic_and_new_export_abandons_old_job(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/"research-profile.md").write_text("profile",encoding="utf-8")
            config=root/"config.toml"
            config.write_text('state_dir = "."\nprofile_file = "research-profile.md"\n[[sources]]\nname = "A"\ntype = "crossref"\nissn = "1234"\n',encoding="utf-8")
            db=pm.db_open(root/"papers.sqlite3")
            for suffix in ("a","b"):
                paper=pm.Paper(f"doi:10.1/{suffix}",f"10.1/{suffix}",suffix,"Abstract","V","2026-01-01","u",[],"s")
                paper.publication_type_raw="journal-article"; paper.publication_type_source="crossref"
                pm.upsert(db,paper,"now")
            db.commit(); db.close()
            with patch("builtins.print") as output: pm.agent_export(config,no_collect=True)
            first=json.loads(output.call_args.args[0])
            with patch("builtins.print") as output: pm.agent_export(config,no_collect=True)
            second=json.loads(output.call_args.args[0]); queue=json.loads(Path(second["queue_path"]).read_text())
            results=[]
            for index,paper in enumerate(queue["papers"]):
                item={"identity":paper["identity"],"relevant":False,"score":0.1,"reasons":"Not related",
                      "matched_themes":[],"confidence":0.9}
                if index==1: item.pop("reasons")
                results.append(item)
            result_path=root/"invalid.json"
            result_path.write_text(json.dumps({"run_id":queue["run_id"],"profile_hash":queue["profile_hash"],
                                               "model":"codex-test","results":results}),encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"missing fields"):
                pm.agent_import(config,result_path)
            db=pm.db_open(root/"papers.sqlite3")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM screenings").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT status FROM agent_jobs WHERE run_id=?",(first["run_id"],)).fetchone()[0],"abandoned")
            self.assertEqual(db.execute("SELECT status FROM agent_jobs WHERE run_id=?",(second["run_id"],)).fetchone()[0],"exported")
            db.close()

    def test_agent_export_snapshots_feedback_and_rejects_profile_drift(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); profile=root/"research-profile.md"; profile.write_text("confirmed",encoding="utf-8")
            config=root/"config.toml"
            config.write_text('state_dir = "."\nprofile_file = "research-profile.md"\n[[sources]]\nname = "A"\ntype = "crossref"\nissn = "1234"\n',encoding="utf-8")
            db=pm.db_open(root/"papers.sqlite3")
            paper=pm.Paper("doi:10.1/x","10.1/x","A","Abstract","V","2026-01-01","u",[],"s")
            paper.publication_type_raw="journal-article"; paper.publication_type_source="crossref"
            pm.upsert(db,paper,"now")
            db.execute("INSERT INTO paper_feedback VALUES(?,?,?,?,?,?,?)",
                       (paper.identity,"interested","Direct transfer",1,"read_later","now","now"))
            db.commit(); db.close()
            with patch("builtins.print") as output:
                pm.agent_export(config,no_collect=True)
            summary=json.loads(output.call_args.args[0]); queue=json.loads(Path(summary["queue_path"]).read_text())
            self.assertEqual(queue["schema_version"],2)
            self.assertEqual(queue["feedback_examples"][0]["reason"],"Direct transfer")
            profile.write_text("unconfirmed edit",encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"confirmed active version"):
                pm.agent_export(config,no_collect=True)

if __name__ == "__main__": unittest.main()
