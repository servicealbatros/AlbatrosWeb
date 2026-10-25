import unittest
from unittest.mock import patch

from automotive_vocabulary import expand_technical_request, canonical_terms
from timing_chain_policy import chain_replacement, default_checklist, expand_chain_scope
from offer_parts_checklist import normalize_lines, pending_lines, render_lines, subtotal
from technician_request_worker_v6_time_parse_fix import detect_operations, tecrmi_row_matches_request


class TimingChainTests(unittest.TestCase):
    def test_chain_scope_and_idempotence(self):
        scope = expand_technical_request('Înlocuire distribuție pe lanț')
        self.assertIn('filtru combustibil', scope)
        self.assertIn('ulei motor original', scope)
        self.assertIn('simering palier', scope)
        self.assertEqual(expand_chain_scope(scope), scope)
        self.assertIn('kit distribuție pe lanț', canonical_terms(scope))
        ids = {op['id'] for op in detect_operations(scope)}
        self.assertIn('timing_chain', ids)
        self.assertNotIn('timing_belt', ids)
        self.assertTrue({'air_filter', 'fuel_filter', 'cabin_filter', 'oil_filter'}.issubset(ids))

    def test_different_jobs_do_not_gain_replacement_parts(self):
        for text in ['Schimb distribuție pe curea', 'Lanț cutie de distribuție',
                     'Verificare distribuție pe lanț', 'Diagnostic lanț distribuție']:
            with self.subTest(text=text):
                self.assertFalse(chain_replacement(text))
                self.assertEqual(default_checklist(text), [])

    def test_intake_cleaning_is_optional(self):
        base = 'Înlocuire distribuție pe lanț'
        self.assertFalse(any('admisie' in r['description'] for r in default_checklist(base)))
        self.assertTrue(any('admisie' in r['description'] for r in default_checklist(base + ', curățare galerie admisie')))
        self.assertFalse(any('admisie' in r['description'] for r in default_checklist(base + ', fără curățare admisie')))

    def test_complete_chain_time_rejects_partial_operation(self):
        op = {'id': 'timing_chain', 'canonical': 'înlocuire set lanțuri distribuție'}
        self.assertTrue(tecrmi_row_matches_request(op, 'Set lanțuri de distribuție înlocuire'))
        self.assertFalse(tecrmi_row_matches_request(op, 'Lanț distribuție superior înlocuire'))


class QuoteLinesTests(unittest.TestCase):
    def setUp(self):
        self.row = dict(description='Garnitură', code='OE123', quantity=2, unit_price_ron=None, confirmed=False)

    def test_unknown_price_stays_empty_and_blocks_confirmation(self):
        self.assertIsNone(normalize_lines([self.row])[0]['unit_price_ron'])
        self.assertTrue(pending_lines([self.row]))
        self.assertIn('value="" placeholder="De confirmat"', render_lines([self.row]))
        with self.assertRaises(ValueError):
            normalize_lines([dict(self.row, confirmed=True)])
        with self.assertRaises(ValueError):
            normalize_lines([dict(self.row, confirmed='false')])

    def test_finite_prices_and_safe_html(self):
        for value in [float('nan'), float('inf'), -1]:
            with self.assertRaises(ValueError):
                normalize_lines([dict(self.row, unit_price_ron=value)])
        html = render_lines([dict(self.row, description='<script>bad</script>', code='" onfocus="bad')])
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)
        ready = dict(self.row, unit_price_ron=12.5, confirmed=True)
        self.assertEqual(subtotal([ready, self.row]), 25)
        self.assertEqual(pending_lines([ready]), [])

    def test_api_preserves_list_and_rejects_unreviewed_offer(self):
        import app
        from fastapi import HTTPException
        stored = {'parts_checklist': [self.row]}
        with patch.object(app, '_request_by_id', return_value={'id': 999, 'symptom': 'Schimb distribuție pe lanț'}), \
             patch.object(app, '_draft_for_request', return_value=stored), \
             patch.object(app, '_save_draft_for_request') as save, \
             patch.object(app, '_require_original_oil_selection'):
            result = app.save_offer_draft(999, app.OfferDraftInput(oil_liters=5.5))
            self.assertIsNone(result['draft']['parts_checklist'][0]['unit_price_ron'])
            self.assertTrue(result['draft']['parts_prices_pending'])
            self.assertEqual(result['draft']['oil_liters'], 6)
            self.assertEqual(save.call_count, 1)
            with self.assertRaises(HTTPException) as exc:
                app._require_advisor_labour_review(999)
            self.assertEqual(exc.exception.status_code, 409)

    def test_new_search_preserves_scope_but_invalidates_prices(self):
        import app
        drafts = {'999': {'parts_checklist': [dict(self.row, unit_price_ron=10, confirmed=True)], 'labour_reviewed': True}}
        with patch.object(app, '_load_offer_drafts', return_value=drafts), \
             patch.object(app, '_save_offer_drafts') as save, \
             patch.object(app, '_load_offer_selections', return_value={}):
            app._clear_offer_work_state(999)
            updated = save.call_args.args[0]['999']
            self.assertEqual(updated['parts_checklist'][0]['code'], 'OE123')
            self.assertIsNone(updated['parts_checklist'][0]['unit_price_ron'])
            self.assertFalse(updated['parts_checklist'][0]['confirmed'])
            self.assertNotIn('labour_reviewed', updated)


if __name__ == '__main__':
    unittest.main()
