from concurrent.futures import CancelledError
from fractions import Fraction
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image, ImageDraw

from pawception.offline import Offline
from pawception.offline_heatmap import PositionCollector, heatmap
from pawception.hotspot_footprint import footprint, occupied_cells
from pawception.files import write_json
from tests.test_live_muse import ManualExecutor


class HeatmapTests(unittest.TestCase):
    def test_duration_weighted_percentages_exclude_gaps_and_unprocessed_time(self):
        result=heatmap([[0,2,0.1,0.1],[2,5,0.9,0.9]],[[0,8]],0,10,2,2)
        self.assertEqual(result['seen_seconds'],5)
        self.assertEqual(result['position_samples'],2)
        self.assertEqual(result['not_detected_seconds'],3)
        self.assertEqual(result['unprocessed_seconds'],2)
        self.assertEqual([cell['percentage'] for cell in result['cells']],[40,0,0,60])

    def test_filter_clips_frame_durations_and_recalculates_denominator(self):
        result=heatmap([[0,4,0,0],[4,8,1,1]],[[0,10]],2,5,2,2)
        self.assertEqual(result['seen_seconds'],3)
        self.assertAlmostEqual(result['cells'][0]['percentage'],200/3)
        self.assertEqual(result['cells'][-1]['first_seen'],4)

    def test_zero_seen_time_has_no_percentage(self):
        result=heatmap([],[[0,4]],0,10)
        self.assertTrue(all(cell['percentage'] is None for cell in result['cells']))
        self.assertEqual(result['unprocessed_seconds'],6)

    def test_empty_range_and_overlapping_coverage_are_rejected(self):
        with self.assertRaises(ValueError):heatmap([],[],1,1)
        with self.assertRaises(ValueError):heatmap([],[[0,8],[5,10]],0,10)

    def test_repeated_stream_masks_replace_one_frame_and_ambiguous_matches_are_excluded(self):
        mask=Image.new('L',(10,10));ImageDraw.Draw(mask).rectangle((2,4,5,7),fill=255)
        collector=PositionCollector((10,10),[(Fraction(0),Fraction(1,2)),(Fraction(1,2),Fraction(1,2))],10)
        with patch('pawception.offline_heatmap.mask_image',return_value=mask):
            record=SimpleNamespace(object_id='pet')
            collector.add(0,[record]);collector.add(0,[record]);collector.add(1,[record,SimpleNamespace(object_id='other')])
        self.assertEqual([point[:5] for point in collector.points()],[[10,10.5,0.4,0.6,10]])
        self.assertEqual(occupied_cells(collector.points()[0][5],2,2),(0,1,2,3))
        self.assertEqual(collector.ambiguous_frames(),1)
        with self.assertRaises(ValueError):collector.add(3,[])

    def test_display_intervals_follow_presentation_times_instead_of_rounded_packet_lengths(self):
        collector=PositionCollector((10,10),[(0,0.49),(0.5,0.49)],10,end=11)
        collector.frames={0:{'pet':(0.2,0.2)},1:{'pet':(0.2,0.2)}}
        result=heatmap(collector.points(),[[10,11]],10,11)
        self.assertEqual(result['seen_seconds'],1)
        self.assertEqual(result['not_detected_seconds'],0)

    def test_footprint_counts_each_occupied_cell_but_seen_time_only_once(self):
        mask=Image.new('L',(10,10));draw=ImageDraw.Draw(mask)
        draw.rectangle((0,0,3,9),fill=255);draw.rectangle((0,6,9,9),fill=255)
        point=[0,4,0.5,0.5,0,footprint(mask)]
        result=heatmap([point],[[0,6]],0,8,2,2)
        self.assertEqual([cell['seconds'] for cell in result['cells']],[4,0,4,4])
        self.assertEqual([cell['percentage'] for cell in result['cells']],[100,0,100,100])
        self.assertEqual(result['seen_seconds'],4)
        self.assertEqual(result['not_detected_seconds'],2)
        self.assertEqual(result['unprocessed_seconds'],2)
        self.assertEqual(result['occupancy_mode'],'footprint')
        filtered=heatmap([point],[[0,6]],2,5,2,2)
        self.assertEqual([cell['seconds'] for cell in filtered['cells']],[2,0,2,2])
        self.assertEqual(filtered['seen_seconds'],2)
        coarse=heatmap([point],[[0,6]],0,8,1,1)
        self.assertEqual(coarse['cells'][0]['seconds'],4)

    def test_footprint_edges_do_not_spill_into_neighbours_and_survive_json(self):
        mask=Image.new('L',(10,10));ImageDraw.Draw(mask).rectangle((5,5,9,9),fill=255)
        saved=json.loads(json.dumps(footprint(mask)))
        self.assertEqual(occupied_cells(saved,2,2),(3,))
        self.assertEqual(occupied_cells(saved,1,1),(0,))
        self.assertIsNone(footprint(Image.new('L',(10,10))))

    def test_empty_sam_update_removes_an_old_footprint(self):
        collector=PositionCollector((10,10),[(0,1)],0,end=1)
        record=SimpleNamespace(object_id='pet')
        with patch('pawception.offline_heatmap.mask_image',return_value=Image.new('L',(10,10),255)):
            collector.add(0,[record])
        with patch('pawception.offline_heatmap.mask_image',return_value=Image.new('L',(10,10))):
            collector.add(0,[record])
        self.assertEqual(collector.points(),[])


class OfflineTests(unittest.TestCase):
    def setup_manager(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        root=Path(temporary.name);executor=ManualExecutor();manager=Offline(root,executor)
        identifier='a'*32;path=root/'data/offline_sources'/identifier/'source.mp4'
        path.parent.mkdir(parents=True);path.write_bytes(b'video')
        metadata={'width':100,'height':100,'duration_seconds':30,'fps':1,'frame_count':30}
        timeline=[(Fraction(i),Fraction(1)) for i in range(30)]
        with patch('pawception.offline.probe_video',return_value=(metadata,timeline,Fraction(1))):
            manager.register(path,identifier,'Test recording')
        return manager,executor,identifier

    def create(self,manager,source,kind='hotspots',**changes):
        return manager.create({'source_id':source,'kind':kind,'provider':'mock','pet_prompt':'cat','batch_frames':10,
            'description':'A dog chewing furniture','window_seconds':10,**changes})

    def test_loading_and_refreshing_make_no_model_calls(self):
        manager,executor,source=self.setup_manager()
        with patch('pawception.offline.OpenAI') as client:
            self.assertEqual(manager.index()['sources'][0]['id'],source)
            manager.index();client.assert_not_called()
        self.assertEqual(executor.calls,[])

    def test_unbundled_long_recording_is_not_advertised_as_available(self):
        manager,_,source=self.setup_manager()
        manifest=manager.data/'candidates/offline_cat_camera_manifest.json'
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({'working':{'file':'candidates/generated/offline/missing.mp4'},
            'source':{'channel':'Cat Momi','webpage_url':'https://example.com/source','license_url':'https://example.com/license'}}))
        self.assertEqual([entry['id'] for entry in manager.sources()],[source])

    def test_change_detection_needs_no_api_key_and_preserves_source_time(self):
        manager,_,source=self.setup_manager()
        with patch('pawception.offline.load_dotenv') as load, patch('pawception.offline.OpenAI') as client:
            job=self.create(manager,source,provider='muse',hotspot_method='changes',pet_prompt='')
            self.assertEqual(job['provider'],'local')
            self.assertEqual(job['hotspot_method'],'changes')
            self.assertEqual(len(job['windows']),1)
            self.assertEqual(job['sampling']['requests'],0)
            load.assert_not_called()
            with patch('pawception.offline.prepare_samples') as prepare, patch('pawception.offline.analyse_changes',return_value=[[0,1,0.2,0.3,0],[20,21,0.6,0.7,20]]):
                manager.run(job['id'],None)
            client.assert_not_called();prepare.assert_called_once()
        result=manager.snapshot(job['id'])
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['heatmap']['seen_seconds'],2)
        self.assertEqual(result['heatmap']['processed_seconds'],30)
        self.assertTrue(result['heatmap']['estimated'])
        self.assertEqual(result['heatmap']['position_samples'],2)
        self.assertEqual(result['heatmap']['sample_count'],30)

    def test_invalid_hotspot_method_is_rejected(self):
        manager,executor,source=self.setup_manager()
        with self.assertRaises(ValueError):self.create(manager,source,hotspot_method='bad')
        self.assertEqual(executor.calls,[])

    def test_change_detection_uses_the_selected_image_size(self):
        manager,_,source=self.setup_manager()
        manager.probes[source][0].update(width=854,height=480)
        job=self.create(manager,source,hotspot_method='changes',change_long_edge=320)
        self.assertEqual(job['change_resolution'],{'width':320,'height':180})
        with patch('pawception.offline.prepare_samples'),patch('pawception.offline.analyse_changes',return_value=[]) as analyse:
            manager.run(job['id'],None)
        self.assertEqual(analyse.call_args.kwargs['size'],(320,180))
        self.assertEqual(manager.snapshot(job['id'])['change_resolution'],{'width':320,'height':180})

    def test_heatmap_square_size_rebins_saved_points_without_resubmitting_analysis(self):
        manager,executor,source=self.setup_manager();job=self.create(manager,source)
        manager.run(job['id'],None)
        fine=manager.snapshot(job['id'],cell_size=20)['heatmap']
        coarse=manager.snapshot(job['id'],cell_size=50)['heatmap']
        self.assertEqual((fine['columns'],fine['rows']),(5,5))
        self.assertEqual((coarse['columns'],coarse['rows']),(2,2))
        self.assertEqual(fine['seen_seconds'],coarse['seen_seconds'])
        self.assertEqual(fine['occupancy_mode'],'footprint')
        self.assertTrue(all(cell['seconds']<=fine['seen_seconds'] for cell in fine['cells']))
        self.assertGreater(sum(cell['seconds'] for cell in fine['cells']),fine['seen_seconds'])
        self.assertEqual(manager.snapshot(job['id'],0,10,cell_size=20)['heatmap']['seen_seconds'],10)
        self.assertEqual(len(executor.calls),1)
        for value in [0,257,20.5,True]:
            with self.assertRaises(ValueError):manager.snapshot(job['id'],cell_size=value)

    def test_saved_background_is_exposed_only_for_its_local_analysis(self):
        manager,_,source=self.setup_manager()
        job=self.create(manager,source,hotspot_method='changes')
        self.assertEqual(job['background_reference_frames'],30)
        self.assertIsNone(job['background_url'])
        path=manager.directory/job['id']/'background.png';Image.new('L',(160,90),40).save(path)
        self.assertEqual(manager.background_image(job['id']),path)
        self.assertEqual(manager.snapshot(job['id'])['background_url'],f"/offline-results/{job['id']}/background.png")
        self.assertIsNone(manager.background_image('../background.png'))
        other=path.with_name('other.png');path.replace(other);path.symlink_to(other)
        self.assertIsNone(manager.background_image(job['id']))

    def test_local_mask_previews_include_negative_frames_and_rebuild_from_saved_footprints(self):
        manager,_,source=self.setup_manager();public=self.create(manager,source,hotspot_method='changes')
        job=manager.jobs[public['id']]
        mask=Image.new('L',(100,100));ImageDraw.Draw(mask).rectangle((10,20,30,40),fill=255)
        job.update(status='completed',points=[[0,1,0.1,0.3,0,footprint(mask)]],completed=[[0,30]])
        result=manager.snapshot(job['id'])
        self.assertEqual(len(result['mask_frames']),30)
        self.assertTrue(result['mask_previews_available'])
        self.assertEqual(result['mask_frames'][0]['matches'][0]['id'],'foreground')
        self.assertEqual(result['mask_frames'][1]['matches'],[])
        self.assertEqual(result['mask_frames'][1]['source_seconds'],1)
        self.assertNotIn('region',result['mask_frames'][0])
        with Image.open(manager.change_frame_image(job['id'],0)) as image:
            self.assertGreater(image.getpixel((20,30))[3],0)
            self.assertEqual(image.getpixel((70,70))[3],0)
        with Image.open(manager.change_frame_image(job['id'],1)) as image:self.assertIsNone(image.getbbox())
        self.assertIsNone(manager.change_frame_image(job['id'],30))
        path=manager.change_frame_image(job['id'],0);other=path.with_name('other.png')
        path.replace(other);path.symlink_to(other)
        self.assertIsNone(manager.change_frame_image(job['id'],0))

    def test_local_previews_are_published_before_analysis_finishes(self):
        manager,_,source=self.setup_manager();public=self.create(manager,source,hotspot_method='changes')
        observed=[]
        def analyse(video,samples,cancelled,*,on_frame_ready,**kwargs):
            on_frame_ready(0,samples[0],None)
            snapshot=manager.snapshot(public['id']);observed.append(snapshot)
            self.assertIsNotNone(manager.change_frame_image(public['id'],0))
            return []
        with patch('pawception.offline.prepare_samples'),patch('pawception.offline.analyse_changes',side_effect=analyse):
            manager.run(public['id'],None)
        self.assertEqual(observed[0]['status'],'running')
        self.assertEqual(len(observed[0]['mask_frames']),1)
        self.assertEqual(observed[0]['heatmap']['processed_seconds'],0)

    def test_local_frame_progress_and_phase_eta_are_available_before_completion(self):
        import time
        manager,_,source=self.setup_manager();public=self.create(manager,source,hotspot_method='changes')
        job=manager.jobs[public['id']];job['status']='running';job['started_at_unix']=time.time()-10
        manager.publish_progress(job,'comparing_frames',10,30)
        job['phase_started_at_unix']=time.time()-2
        result=manager.snapshot(job['id'])
        self.assertEqual(result['local_progress'],{'stage':'comparing_frames','completed':10,'total':30})
        self.assertAlmostEqual(result['estimated_remaining_seconds'],4,delta=0.2)
        manager.publish_progress(job,'building_background',10,30)
        job['phase_started_at_unix']=time.time()-2
        result=manager.snapshot(job['id'])
        self.assertIsNone(result['estimated_remaining_seconds'])
        self.assertAlmostEqual(result['estimated_stage_remaining_seconds'],4,delta=0.2)
        job['status']='stopped'
        self.assertIsNone(manager.snapshot(job['id'])['estimated_remaining_seconds'])

    def test_only_registered_sam_frame_files_can_be_served(self):
        manager,_,source=self.setup_manager();public=self.create(manager,source)
        job=manager.jobs[public['id']];job['sam_frames']={'0000:2':{}}
        folder=manager.directory/job['id']/'windows/0000/sam/frames';folder.mkdir(parents=True)
        path=folder/'000002.png';Image.new('RGBA',(10,10)).save(path)
        self.assertEqual(manager.sam_frame_image(job['id'],'0000',2),path)
        self.assertIsNone(manager.sam_frame_image(job['id'],'0000',1))
        self.assertIsNone(manager.sam_frame_image(job['id'],'9999',2))
        other=path.with_name('other.png');path.replace(other);path.symlink_to(other)
        self.assertIsNone(manager.sam_frame_image(job['id'],'0000',2))

    def test_mock_hotspots_persist_and_filters_do_not_submit_work(self):
        manager,executor,source=self.setup_manager()
        job=self.create(manager,source)
        manager.run(job['id'],None)
        result=manager.snapshot(job['id'])
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['heatmap']['seen_seconds'],23)
        self.assertEqual(result['heatmap']['not_detected_seconds'],7)
        self.assertEqual(result['heatmap']['unprocessed_seconds'],0)
        filtered=manager.snapshot(job['id'],0,10)
        self.assertEqual(filtered['heatmap']['seen_seconds'],10)
        self.assertEqual(len(executor.calls),1)
        restored=Offline(manager.root,ManualExecutor())
        self.assertEqual(restored.snapshot(job['id'])['heatmap'],result['heatmap'])

    def test_new_search_retains_previous_queries_and_source_offsets(self):
        manager,_,source=self.setup_manager()
        first=self.create(manager,source,'events',start_seconds=10,end_seconds=20)
        manager.run(first['id'],None)
        self.assertEqual(manager.snapshot(first['id'])['detections'][0]['start'],12)
        second=self.create(manager,source,'events',description='A pet on the sofa')
        manager.run(second['id'],None)
        self.assertEqual(len(manager.index()['runs']),2)
        self.assertEqual(manager.snapshot(first['id'])['description'],'A dog chewing furniture')

    def test_negative_results_keep_model_evidence_and_original_window_times(self):
        manager,_,source=self.setup_manager()
        job=self.create(manager,source,'events',start_seconds=10,end_seconds=20)
        def worker(job,window,metadata):
            job['records'][window['id']]={'status':'completed','output':{'event_1':{
                'detected':False,'intervals':[],'evidence':'The pet remains on the floor.'}}}
            window['status']='completed'
        with patch.object(manager,'mock_window',side_effect=worker):manager.run(job['id'],None)
        result=manager.snapshot(job['id'])
        self.assertEqual(result['detections'],[])
        self.assertEqual(result['observations'],[{'event_id':result['events'][0]['id'],'window_id':'0000',
            'start_seconds':10,'end_seconds':20,'detected':False,'evidence':'The pet remains on the floor.'}])

    def test_overlapping_search_windows_union_positive_intervals_without_double_counting(self):
        manager,_,source=self.setup_manager()
        job=self.create(manager,source,'events',window_seconds=10,overlap_seconds=5,end_seconds=20)
        def worker(job,window,metadata):
            job['records'][window['id']]={'status':'completed','output':{'event_1':{
                'detected':True,'intervals':[{'start':0,'end':window['end_seconds']-window['start_seconds']}],
                'evidence':'Mock overlapping positive'}}}
            window['status']='completed'
        with patch.object(manager,'mock_window',side_effect=worker):manager.run(job['id'],None)
        intervals=manager.snapshot(job['id'])['detections']
        self.assertEqual(len(intervals),1)
        self.assertEqual((intervals[0]['start'],intervals[0]['end'],intervals[0]['detected_seconds']),(0,20,20))

    def test_multiple_events_default_to_auto_and_keep_separate_results(self):
        manager,_,source=self.setup_manager()
        job=self.create(manager,source,'events',events=[{'name':'Barking','description':'A dog barking'},
            {'name':'Chewing','description':'A dog chewing furniture'}])
        self.assertEqual([event['input_mode'] for event in job['events']],['auto','auto'])
        manager.run(job['id'],None)
        result=manager.snapshot(job['id'])
        self.assertEqual([event['resolved_mode'] for event in result['events']],['audio','visual'])
        self.assertEqual({part['event_id'] for part in result['detections']},{event['id'] for event in result['events']})
        self.assertEqual([event['name'] for event in result['events']],['Barking','Chewing'])

    def test_empty_or_invalid_event_lists_are_rejected_before_work(self):
        manager,executor,source=self.setup_manager()
        for events in [[],[{'name':'Missing description'}],[{'name':'X','description':'X','input_mode':'wrong'}]]:
            with self.assertRaises(ValueError):self.create(manager,source,'events',events=events)
        self.assertEqual(executor.calls,[])

    def test_busy_worker_is_bounded_and_stop_prevents_new_work(self):
        manager,_,source=self.setup_manager()
        job=self.create(manager,source)
        with self.assertRaises(ValueError):self.create(manager,source)
        manager.stop(job['id'])
        with patch('pawception.offline.OpenAI') as client:
            manager.run(job['id'],None);client.assert_not_called()
        self.assertEqual(manager.snapshot(job['id'])['status'],'stopped')

    def test_failed_tracking_window_remains_unprocessed_and_keeps_prior_results(self):
        manager,_,source=self.setup_manager()
        job=self.create(manager,source)
        original=manager.mock_window
        def worker(job,window,metadata):
            if window['id']=='0001':raise ValueError('Test failure')
            original(job,window,metadata)
        with patch.object(manager,'mock_window',side_effect=worker):manager.run(job['id'],None)
        result=manager.snapshot(job['id'])
        self.assertEqual(result['status'],'error')
        self.assertEqual(result['heatmap']['processed_seconds'],10)
        self.assertEqual(result['heatmap']['unprocessed_seconds'],20)

    def test_stop_during_tracking_keeps_completed_coverage_and_cancels_the_active_window(self):
        manager,_,source=self.setup_manager();job=self.create(manager,source)
        original=manager.mock_window
        def worker(job,window,metadata):
            if window['id']=='0001':raise CancelledError()
            original(job,window,metadata)
        with patch.object(manager,'mock_window',side_effect=worker):manager.run(job['id'],None)
        result=manager.snapshot(job['id'])
        self.assertEqual(result['status'],'stopped')
        self.assertEqual([window['status'] for window in result['windows']],['completed','cancelled','cancelled'])
        self.assertEqual(result['heatmap']['processed_seconds'],10)

    def test_restart_marks_active_job_interrupted_without_restarting_calls(self):
        manager,_,source=self.setup_manager();job=self.create(manager,source)
        executor=ManualExecutor();restored=Offline(manager.root,executor)
        self.assertEqual(restored.snapshot(job['id'])['status'],'interrupted')
        self.assertEqual(executor.calls,[])

    def test_event_results_are_visible_before_upload_cleanup_finishes(self):
        manager,_,source=self.setup_manager();job=self.create(manager,source,'events')
        saved=manager.jobs[job['id']];saved['status']='running';saved['windows'][0]['status']='running'
        folder=manager.directory/job['id']/'windows/0000';folder.mkdir(parents=True)
        write_json(folder/'live.progress.json',{'record':{'output':{'event_1':{
            'detected':True,'intervals':[{'start':2,'end':6}],'evidence':'Validated test'}}}})
        self.assertEqual(manager.snapshot(job['id'])['detections'][0]['start'],2)

    def test_invalid_ranges_models_and_symlinks_are_rejected_before_any_request(self):
        manager,executor,source=self.setup_manager()
        for settings in ({'start_seconds':10,'end_seconds':5},{'end_seconds':31},{'model':'unknown'},
                         {'window_seconds':2,'overlap_seconds':2,'kind':'events'},{'start_seconds':float('nan')}):
            with self.assertRaises(ValueError):self.create(manager,source,**settings)
        entry,path=manager.source(source);other=path.with_name('other.mp4');other.write_bytes(b'other')
        path.unlink();path.symlink_to(other)
        with self.assertRaises(ValueError):manager.source(source)
        self.assertEqual(executor.calls,[])

    def test_cancelled_sam_request_still_deletes_its_upload(self):
        manager,_,source=self.setup_manager()
        directory=manager.root/'request';directory.mkdir();(directory/'clip.mp4').write_bytes(b'video')
        window={'id':'0000','file':'clip.mp4','start_seconds':0}
        client=MagicMock();client.__enter__.return_value=client
        client.files.create.return_value.id='test-file';client.files.delete.return_value.deleted=True
        from threading import Event
        with patch('pawception.offline.OpenAI',return_value=client), \
             patch('pawception.offline.ffmpeg'), \
             patch('pawception.offline.probe_video',return_value=({'width':100,'height':100},[(0,1)],1)), \
             patch('pawception.offline.segment_video',side_effect=CancelledError):
            with self.assertRaises(CancelledError):manager.track(directory,window,'cat','test',Event())
        client.files.delete.assert_called_once_with('test-file')

    def test_sam_frame_progress_and_masks_arrive_before_the_batch_finishes(self):
        from threading import Event
        from tests.test_sam_streaming import records,FRAME
        manager,_,_=self.setup_manager()
        directory=manager.root/'request';directory.mkdir();(directory/'clip.mp4').write_bytes(b'video')
        window={'id':'0000','file':'clip.mp4','start_seconds':10,'end_seconds':10.6}
        client=MagicMock();client.__enter__.return_value=client
        client.files.create.return_value.id='test-file';client.files.delete.return_value.deleted=True
        progress,frames=[],[]
        def stream(*args,on_frame,**kwargs):
            on_frame(2,records());on_frame(2,records())
            self.assertEqual(progress[-1][1],1)  # Same frame twice is still one frame.
            on_frame(0,records()+records(FRAME.replace('<0f>0','<0f>1')))
            self.assertEqual(progress[-1][1],2)
            self.assertTrue((directory/'windows/0000/sam/frames/000002.png').is_file())
            client.files.delete.assert_not_called()
        with patch('pawception.offline.OpenAI',return_value=client),patch('pawception.offline.ffmpeg'), \
             patch('pawception.offline.probe_video',return_value=({'width':490,'height':480},[(0,0.2),(0.2,0.2),(0.4,0.2)],1)), \
             patch('pawception.offline.segment_video',side_effect=stream):
            points,ambiguous=manager.track(directory,window,'cat','test',Event(),
                on_progress=lambda *value:progress.append(value),on_frame_ready=frames.append)
        self.assertEqual(progress[-1],('batch_complete',3,3))
        self.assertEqual(len(points),1)
        self.assertEqual(ambiguous,1)
        self.assertEqual(frames[0]['source_seconds'],10.4)
        self.assertEqual(len(frames[-1]['matches']),2)
        client.files.delete.assert_called_once_with('test-file')

    def test_frame_progress_accumulates_across_batches_in_snapshots(self):
        manager,_,source=self.setup_manager();job=self.create(manager,source)
        manager.jobs[job['id']]['provider']='muse'
        def track(directory,window,prompt,key,cancelled,**callbacks):
            callbacks['on_progress']('receiving',2,10)
            self.assertEqual(manager.snapshot(job['id'])['frame_progress']['received'],window['index']*10+2)
            callbacks['on_progress']('receiving',2,10)
            self.assertEqual(manager.snapshot(job['id'])['frame_progress']['received'],window['index']*10+2)
            callbacks['on_result_ready']([],0)
            return [],0
        with patch('pawception.offline.prepare_samples'),patch.object(manager,'track',side_effect=track):
            manager.run(job['id'],'test')
        result=manager.snapshot(job['id'])
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['frame_progress']['received'],30)
        self.assertEqual(result['frame_progress']['total'],30)
