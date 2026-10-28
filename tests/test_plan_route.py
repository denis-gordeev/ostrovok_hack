import unittest
from unittest.mock import patch

from geo_tools import plan_route
from map_tools import collection, map_result


class PlanRouteTests(unittest.TestCase):
    def run_plan(self, length=1000, modes=None, walk_error=False, transit=True, regional=False):
        def walk(**kwargs):
            if walk_error:
                raise ValueError('unavailable')
            return map_result({'length_m': length}, {'default': collection([])})

        itineraries = ([{'contains_transit': False, 'total_minutes_from_requested_departure': 1},
                        {'contains_transit': True, 'total_minutes_from_requested_departure': 30},
                        {'contains_transit': True, 'total_minutes_from_requested_departure': 20}]
                       if transit else [])
        with patch('geo_tools.walking_route.func', side_effect=walk), \
             patch('geo_tools.driving_route.func', return_value=map_result({'length_m': 20000}, {'default': collection([])})), \
             patch('geo_tools.transit_route.func', return_value=map_result({'itineraries': itineraries},
                   {f'option-{i}': collection([]) for i in range(1, len(itineraries)+1)})), \
             patch('geo_tools.ox.distance.great_circle', return_value=11000 if regional else 500):
            return plan_route.func(55.8, 49.1, 55.81, 49.11, modes=modes)

    def test_threshold_and_transit_ranking(self):
        result, artifact = self.run_plan()
        self.assertEqual(result['primary_variant'], 'transit-option-3')
        self.assertIn(result['primary_variant'], artifact['collections'])
        self.assertEqual(self.run_plan(length=999)[0]['primary_mode'], 'walk')

    def test_failure_preserves_transit(self):
        result, _ = self.run_plan(walk_error=True)
        self.assertIn('error', result['results']['walk'])
        self.assertEqual(result['primary_mode'], 'transit')

    def test_regional_and_explicit_modes(self):
        self.assertEqual(set(self.run_plan(regional=True)[0]['results']), {'car', 'transit'})
        self.assertEqual(self.run_plan(modes=['car'])[0]['primary_mode'], 'car')
        self.assertEqual(self.run_plan(modes=['transit'], transit=False)[0]['primary_mode'], None)
        self.assertEqual(self.run_plan(transit=False)[0]['primary_mode'], 'walk')

    def test_empty_modes_rejected(self):
        with self.assertRaises(ValueError):
            self.run_plan(modes=[])
