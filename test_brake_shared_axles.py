import unittest
import app


class SharedAxleTests(unittest.TestCase):
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
