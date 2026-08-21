"""Community discovery cards in Social are rows, and Back returns to Social."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text()
CSS = (ROOT / "static/css/client.css").read_text()


class CommunityTimelineNavigation(unittest.TestCase):
    def test_definitions_are_discovered_in_communities_not_social(self):
        filt = APP[APP.index("function _tlFilter(view){"):][:900]
        self.assertIn("ev.kind!==34550", filt)
        queries = APP[APP.index("function timelineFilter(){"):][:1100]
        self.assertNotIn("30023,34550", queries)

    def test_timeline_community_is_not_a_full_width_video_card(self):
        self.assertIn(".tl-notes>.community-card{display:grid", CSS)
        self.assertIn("grid-template-columns:min(180px,28%) minmax(0,1fr)", CSS)
        self.assertIn(".tl-notes>.community-card .stream-thumb{aspect-ratio:auto", CSS)

    def test_opening_stamps_social_and_pushes_a_community_entry(self):
        fn = APP[APP.index("async function openCommunity(e, routed){"):][:1000]
        self.assertIn("_navView('community')", fn)
        self.assertIn("community:e.id", fn)

    def test_back_pops_the_social_entry(self):
        fn = APP[APP.index("async function openCommunity(e, routed){"):][:2400]
        self.assertIn("if(_navPushed>0)", fn)
        self.assertIn("history.back()", fn)

    def test_forward_can_rebuild_the_community(self):
        pop = APP[APP.index("window.addEventListener('popstate'"):][:1500]
        self.assertIn("st.pcv==='community'", pop)
        self.assertIn("openCommunity(c, true)", pop)


if __name__ == "__main__":
    unittest.main()
