import unittest
import app
from autototal_request_worker import _oe_vin_targets, _oe_option_allowed, _rank_standard_models
from technician_request_worker_v6_time_parse_fix import detect_operations


class SharedAxleTests(unittest.TestCase):
    def test_protection_plate_and_combined_package_are_not_standalone_parts(self):
        disc = {'requested_item':'discuri frână spate'}
        self.assertFalse(_oe_option_allowed(disc, 'placa protectie disc frana', 'OE'))
        self.assertTrue(_oe_option_allowed(disc, 'Disc de frână 302X12MM', 'OE'))
        pads = {'requested_item':'plăcuțe frână față'}
        self.assertFalse(_oe_option_allowed(pads, '1 set discuri de frână cu plăcuţe de frână', 'OE'))
        self.assertTrue(_oe_option_allowed(pads, 'SET PLACUTE FRANA TEXTAR', 'TEXTAR'))

    def test_vin_planner_preserves_both_axles_and_localized_names(self):
        rows = _oe_vin_targets('Discuri de frana fata si spate si placute de frana fata si spate')
        discs = [r for r in rows if r['requested_item'].startswith('discuri')]
        pads = [r for r in rows if r['requested_item'].startswith('plăcuțe')]
        self.assertEqual(len(discs), 2)
        self.assertEqual(len(pads), 2)
        self.assertTrue(any(r.get('front') for r in discs))
        self.assertTrue(any(r.get('rear') for r in discs))
        self.assertTrue(all('disc de frana' in r['terms'] for r in discs))

    def test_adjacent_brake_components_share_the_trailing_axles(self):
        for text in ('discuri placute fata spate', 'discuri si placute fata si spate', 'placute si discuri fata/spate'):
            self.assertEqual(set(app._required_offer_part_kinds(text)),
                             {'brake_pads', 'brake_pads_rear', 'brake_discs_front', 'brake_discs_rear'}, text)

    def test_explicit_separate_axles_remain_separate(self):
        self.assertEqual(set(app._required_offer_part_kinds('discuri spate si placute fata')),
                         {'brake_discs_rear', 'brake_pads'})
        self.assertEqual(set(app._required_offer_part_kinds('discuri fata si placute spate')),
                         {'brake_discs_front', 'brake_pads_rear'})

    def test_labour_preserves_shared_axles(self):
        for text in ('discuri placute fata spate', 'discuri si placute fata si spate',
                     'placute si discuri fata/spate'):
            self.assertEqual({row['id'] for row in detect_operations(text)},
                             {'brake_discs_front', 'brake_discs_rear',
                              'brake_pads_front', 'brake_pads_rear'}, text)

    def test_standard_model_uses_chassis_instead_of_longest_name(self):
        models = [{'rangeNameField': 'A6 Allroad C6 (4FH)'},
                  {'rangeNameField': 'A6 Avant C6 (4F5)'},
                  {'rangeNameField': 'A6 C6 (4F2)'}]
        self.assertEqual(_rank_standard_models(models, 'A6 (4F2, C6)')[0]['rangeNameField'],
                         'A6 C6 (4F2)')

    def test_localized_vag_pad_rows_are_not_caliper_seals_or_disc_packages(self):
        pads = {'requested_item': 'plăcuțe frână față'}
        for title in ('1 set garnituri frane cu indi- cator uzura pentru frane cu discuri',
                      '1 set garnituri frane pt. frane cu discuri',
                      'Repair kit, brake pads asbestos-free'):
            self.assertTrue(_oe_option_allowed(pads, title, 'OE'), title)
        for title in ('Set garnituri carcasă etrier', '1 set discuri de frână cu plăcuţe de frână'):
            self.assertFalse(_oe_option_allowed(pads, title, 'OE'), title)

    def test_sensors_are_required_by_vin_evidence_rather_than_make(self):
        request = {'symptom': 'placute fata spate', 'vehicle': 'AUDI A6'}
        pads = [{'requested_item': 'placute frana fata'}, {'requested_item': 'placute frana spate'}]
        self.assertEqual(app._missing_requested_parts(request, pads), [])
        sensors = [{'requested_item': 'senzor uzura placute frana fata', 'position': 'FATA'}]
        self.assertEqual(app._missing_requested_parts(request, pads, sensors),
                         ['senzor uzură plăcuțe frână față'])


if __name__ == '__main__':
    unittest.main()
