import unittest
from unittest.mock import patch

from app import _required_offer_part_kinds, _require_advisor_labour_review
from autototal_request_worker import (
    _required_product_kinds, _independent_plan, _oe_vin_targets,
    _history_search_candidates, _revision_product_kind,
)
from offer_parts_checklist import line_gaps, render_lines
from offer_presentation import render_parts_workspace


class OfferScopeValidationTests(unittest.TestCase):
    def test_transmission_does_not_require_or_search_engine_service(self):
        for query in (
            'Schimb ulei cutie de viteze automată GA8HP51X cu kit baie și filtru; '
            'schimb ulei cutie de transfer ATC13; schimb ulei diferențial față și spate',
            'Revizie cutie automată cu baie și filtru',
            'Schimb ulei grupuri față și spate',
        ):
            with self.subTest(query=query):
                self.assertEqual(_required_offer_part_kinds(query), {})
                self.assertEqual(_required_product_kinds(query), set())
                self.assertEqual(_independent_plan(query), [])
                self.assertEqual(_oe_vin_targets(query), [])
                self.assertEqual(_history_search_candidates({'_history_parts': [
                    dict(requested_item='filtru aer', part_code='ENGINE123', brand='BMW'),
                ]}, query), [])
        self.assertNotEqual(_revision_product_kind('ulei grup spate'), 'engine_oil')
        self.assertNotEqual(_revision_product_kind('ulei cutia de transfer'), 'engine_oil')

    def test_engine_service_and_chain_rule_still_include_filters(self):
        required = {'oil_filter', 'air_filter', 'cabin_filter', 'fuel_filter',
                    'engine_oil', 'oil_drain_plug'}
        for query in ('Revizie motor ulei și filtre; schimb ulei cutie automată',
                      'Schimb distribuție pe lanț', 'Revizie completă ulei și filtre'):
            with self.subTest(query=query):
                self.assertTrue(required <= set(_required_offer_part_kinds(query)))
                self.assertTrue(required <= _required_product_kinds(query))
                self.assertIn('filtru aer', {r['requested_item'] for r in _independent_plan(query)})
        self.assertTrue(_independent_plan('Filtru habitaclu'))

    def test_partial_identification_names_actual_missing_fields(self):
        rows = [dict(description='O-ring injector', code='', quantity=4,
                     unit_price_ron=None, confirmed=False)]
        self.assertEqual(line_gaps(rows), [dict(description='O-ring injector',
                         missing=['cod piesă', 'preț unitar', 'verificarea consilierului'])])
        html = render_lines(rows)
        self.assertIn('Lipsește: cod piesă, preț unitar', html)
        self.assertIn('placeholder="De confirmat"', html)
        with patch('app._request_by_id', return_value={'symptom':'Schimb O-ring injector'}), \
             patch('app._require_original_oil_selection'), patch('app._load_offer_selections', return_value={}), \
             patch('app._draft_for_request', return_value={'parts_checklist': rows}):
            from fastapi import HTTPException
            with self.assertRaises(HTTPException) as error:
                _require_advisor_labour_review(999)
        self.assertIn('O-ring injector: cod piesă, preț unitar', error.exception.detail)

    def test_checklist_is_visible_even_without_supplier_alternatives(self):
        rows = [dict(description='Kit baie cutie', code='BMW123', quantity=1,
                     unit_price_ron=100, confirmed=False)]
        html = render_parts_workspace([], [], checklist=rows)
        self.assertIn('Necesar editabil: 1 poziții', html)
        self.assertIn('1 de verificat', html)
        self.assertIn('Necesar: 1 poziții', html)
        self.assertNotIn('0 / 0', html)
        self.assertIn('nu dovedește identificarea automată', html)


if __name__ == '__main__':
    unittest.main()
