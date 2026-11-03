import importlib.util
import unittest


@unittest.skipUnless(importlib.util.find_spec('numpy'), 'optional MoGe environment')
class SpatialHotspotsTests(unittest.TestCase):
    def setUp(self):
        import numpy as np
        from pawception.spatial_hotspots import PetVoxels, backproject, surface_mesh
        self.np, self.Voxels = np, PetVoxels
        self.project, self.mesh = backproject, surface_mesh
        self.k = np.array([[1., 0., .5], [0., 1., .5], [0., 0., 1.]])

    def test_mesh_does_not_bridge_holes_or_depth_jumps(self):
        rgb = self.np.zeros((2, 2, 3))
        self.assertEqual(len(self.mesh(self.np.ones((2, 2)), rgb, self.k)['triangles']), 6)
        self.assertEqual(self.mesh(self.np.array([[1., 4.], [1., 4.]]), rgb, self.k)['triangles'], [])
        self.assertEqual(self.mesh(self.np.array([[1., 0.], [0., 1.]]), rgb, self.k)['triangles'], [])

    def test_removes_boundaries_that_old_absolute_threshold_kept(self):
        rgb = self.np.zeros((2, 2, 3))
        for depth in [self.np.array([[2., 2.1], [2., 2.1]]),
                      self.np.array([[.5, .54], [.5, .54]])]:
            with self.subTest(depth=depth.tolist()):
                self.assertEqual(self.mesh(depth, rgb, self.k)['triangles'], [])

    def test_preserves_gentle_surfaces_and_leaves_a_gap_at_step(self):
        depth = self.np.tile(self.np.linspace(2., 2.02, 8), (6, 1))
        rgb = self.np.zeros((6, 8, 3))
        self.assertEqual(len(self.mesh(depth, rgb, self.k)['triangles'])//3, 2*5*7)
        depth[:, 4:] += .2
        mesh = self.mesh(depth, rgb, self.k)
        self.assertGreater(len(mesh['triangles']), 0)
        points = self.np.array(mesh['positions']).reshape(-1, 3)
        for triangle in self.np.array(mesh['triangles']).reshape(-1, 3):
            self.assertLess(self.np.ptp(points[triangle, 2]), .04)

    def test_invalid_depth_cannot_create_nonfinite_vertices(self):
        depth = self.np.ones((6, 6))
        depth[2, 2], depth[3, 3] = self.np.nan, self.np.inf
        mesh = self.mesh(depth, self.np.zeros((6, 6, 3)), self.k)
        self.assertTrue(self.np.isfinite(mesh['positions']).all())

    def test_voxels_count_time_once_per_sample_and_preserve_gaps(self):
        voxels = self.Voxels(size=.5)
        depth = self.np.full((10, 10), .1)
        pet = self.np.ones_like(depth, dtype=bool)
        voxels.add(depth, pet, self.k, {'start':20., 'end':22., 'frame_seconds':21.})
        voxels.add(depth, pet, self.k, {'start':22., 'end':23., 'frame_seconds':22.5})
        voxels.add(depth, pet, self.k, {'start':30., 'end':34., 'frame_seconds':32.})
        self.assertEqual(voxels.seen, [[20., 23.], [30., 34.]])
        self.assertEqual(len(voxels.export()), 1)
        for cell in voxels.export():
            self.assertEqual(cell['intervals'], [[20., 23.], [30., 34.]])
            self.assertEqual(cell['first_seen'], 21.)

    def test_centre_uses_median_and_total_voxel_time_equals_seen_time(self):
        voxels = self.Voxels(size=.5)
        pet = self.np.ones((1, 5), dtype=bool)
        for start, depth in [(0., [1., 1., 1., 1., 40.]), (2., [2., 2., 2., 2., 40.])]:
            voxels.add(self.np.array([depth]), pet, self.k, {'start':start, 'end':start+2., 'frame_seconds':start+1.})
        cells = voxels.export()
        self.assertEqual(len(cells), 2)
        self.assertEqual(sum(b-a for cell in cells for a,b in cell['intervals']), 4.)
        self.assertEqual(sorted(cell['key'][2] for cell in cells), [2, 4])

    def test_invalid_depth_is_not_seen_but_is_processed(self):
        voxels = self.Voxels()
        voxels.add(self.np.array([[self.np.nan]]), self.np.array([[True]]), self.k,
                   {'start':0., 'end':5., 'frame_seconds':2.})
        self.assertEqual(voxels.seen, [])
        self.assertEqual(voxels.processed, [[0., 5.]])

    def test_backprojection_uses_pixel_centres_and_normalized_intrinsics(self):
        cloud = self.project(self.np.full((2, 2), 2.), self.k)
        self.np.testing.assert_allclose(cloud[0, 0], [-.5, -.5, 2.])


if __name__ == '__main__':
    unittest.main()
