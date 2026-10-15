import unittest
import app
from autototal_request_worker import _oe_vin_targets


class SharedAxleTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
