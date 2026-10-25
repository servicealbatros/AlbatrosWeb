import unittest
from unittest.mock import patch
from transmission_service_policy import drivetrain_operations, engine_service_requested
from technician_request_worker_v6_time_parse_fix import detect_operations, tecrmi_row_matches_request


class TransmissionServiceTests(unittest.TestCase):
    def test_complete_xdrive_scope_is_not_engine_revision(self):
        text = 'Revizie: schimb ulei cutie de viteze automată cu baie și filtru, ulei cutie transfer și grupuri față, spate'
        ids = {row['id'] for row in detect_operations(text)}
        self.assertEqual(ids, {'automatic_transmission_service', 'transfer_case_oil', 'differential_oil_front', 'differential_oil_rear'})
        self.assertFalse(engine_service_requested(text))

    def test_axles_and_xdrive_never_invent_unrequested_work(self):
        ids = {row['id'] for row in drivetrain_operations('Schimb ulei diferențial spate BMW xDrive')}
        self.assertEqual(ids, {'differential_oil_rear'})
        self.assertEqual(drivetrain_operations('Schimb ulei cutie manuală'), [])
        self.assertNotIn('engine_oil', {row['id'] for row in detect_operations('Schimb ulei cutie manuală')})
        self.assertEqual(drivetrain_operations('Distribuție pe lanț motor'), [])
        self.assertEqual({row['id'] for row in detect_operations('Ulei cutia de distribuție înlocuire')}, {'transfer_case_oil'})

    def test_mixed_service_keeps_explicit_engine_oil(self):
        text = 'Schimb ulei motor și filtre; schimb ulei transmisie automată'
        ids = {row['id'] for row in detect_operations(text)}
        self.assertIn('engine_oil', ids)
        self.assertIn('automatic_transmission_oil', ids)
        self.assertTrue(engine_service_requested(text))
        self.assertTrue(engine_service_requested('Schimb distribuție pe lanț'))

    def test_catalogue_times_reject_wrong_component_and_axle(self):
        self.assertTrue(tecrmi_row_matches_request({'id': 'transfer_case_oil'}, 'Ulei cutia de distribuție înlocuire'))
        self.assertFalse(tecrmi_row_matches_request({'id': 'transfer_case_oil'}, 'Lanț de distribuție înlocuire'))
        self.assertFalse(tecrmi_row_matches_request({'id': 'differential_oil_front'}, 'Ulei diferențial spate înlocuire'))
        self.assertTrue(tecrmi_row_matches_request({'id': 'automatic_transmission_service'}, 'Unitate formată din baia de ulei pentru cutia de viteze automată și filtru înlocuire'))

    def test_transmission_draft_can_be_reviewed_without_engine_oil_or_filters(self):
        import app
        symptom = 'Schimb ulei transmisie automată cu baie și filtru; ulei cutie transfer și diferențiale față/spate'
        operations = [dict(id=o['id'], name=o['canonical'], hours=0.5, source='Catalog test') for o in drivetrain_operations(symptom)]
        state = {}
        def save(_id, draft):
            state.update(draft)
        with patch.object(app, '_request_by_id', return_value=dict(id=999, symptom=symptom)), \
             patch.object(app, '_draft_for_request', side_effect=lambda _: dict(state)), \
             patch.object(app, '_save_draft_for_request', side_effect=save), \
             patch.object(app, '_load_offer_selections', return_value={}):
            result = app.save_offer_draft(999, app.OfferDraftInput(
                parts_checklist=[dict(description='ATF cutie',code='ATF123',quantity=1,unit_price_ron=10,confirmed=True)],
                labour_operations=operations,technical_hours=2,expected_hours=2.4,booking_hours=3.4,
                labour_price_ron=480,labour_reviewed=True))
            self.assertEqual(result['draft']['oil_liters'], 0)
            self.assertEqual(result['draft']['total_ron'], 490)
            self.assertEqual(app._missing_draft_labour_operations(dict(symptom=symptom), state), [])
            app._require_advisor_labour_review(999)


if __name__ == '__main__':
    unittest.main()
