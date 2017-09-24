from django.db.models import F
from django.conf import settings

from celery.task import task
from celery import chain, group, chord
from celery.utils.log import get_task_logger
from datetime import datetime, timedelta
import shutil
import xarray as xr
import numpy as np
import os
import imageio
from collections import OrderedDict

from utils.data_cube_utilities.data_access_api import DataAccessApi
from utils.data_cube_utilities.dc_utilities import (create_cfmask_clean_mask, create_bit_mask, write_geotiff_from_xr, write_png_from_xr,
                                write_single_band_png_from_xr, add_timestamp_data_to_xr, clear_attrs)
from utils.data_cube_utilities.dc_chunker import (create_geographic_chunks, group_datetimes_by_month, combine_geographic_chunks)
from utils.data_cube_utilities.dc_ndvi_anomaly import compute_ndvi_anomaly
from apps.dc_algorithm.utils import create_2d_plot

from .models import NdviAnomalyTask
from apps.dc_algorithm.models import Satellite
from apps.dc_algorithm.tasks import DCAlgorithmBase

logger = get_task_logger(__name__)


class BaseTask(DCAlgorithmBase):
    app_name = 'ndvi_anomaly'


@task(name="ndvi_anomaly.run", base=BaseTask)
def run(task_id=None):
    """Responsible for launching task processing using celery asynchronous processes

    Chains the parsing of parameters, validation, chunking, and the start to data processing.
    """
    chain(
        parse_parameters_from_task.s(task_id=task_id),
        validate_parameters.s(task_id=task_id),
        perform_task_chunking.s(task_id=task_id),
        start_chunk_processing.s(task_id=task_id))()
    return True


@task(name="ndvi_anomaly.parse_parameters_from_task", base=BaseTask)
def parse_parameters_from_task(task_id=None):
    """Parse out required DC parameters from the task model.

    See the DataAccessApi docstrings for more information.
    Parses out platforms, products, etc. to be used with DataAccessApi calls.

    If this is a multisensor app, platform and product should be pluralized and used
    with the get_stacked_datasets_by_extent call rather than the normal get.

    Returns:
        parameter dict with all keyword args required to load data.

    """
    task = NdviAnomalyTask.objects.get(pk=task_id)

    parameters = {
        'platform': task.satellite.datacube_platform,
        'time': (task.time_start, task.time_end),
        'longitude': (task.longitude_min, task.longitude_max),
        'latitude': (task.latitude_min, task.latitude_max),
        'measurements': task.measurements
    }

    parameters['product'] = Satellite.objects.get(
        datacube_platform=parameters['platform']).product_prefix + task.area_id

    task.execution_start = datetime.now()
    task.update_status("WAIT", "Parsed out parameters.")

    return parameters


@task(name="ndvi_anomaly.validate_parameters", base=BaseTask)
def validate_parameters(parameters, task_id=None):
    """Validate parameters generated by the parameter parsing task

    All validation should be done here - are there data restrictions?
    Combinations that aren't allowed? etc.

    Returns:
        parameter dict with all keyword args required to load data.
        -or-
        updates the task with ERROR and a message, returning None

    """
    task = NdviAnomalyTask.objects.get(pk=task_id)
    dc = DataAccessApi(config=task.config_path)

    acquisitions = dc.list_acquisition_dates(**parameters)

    if len(acquisitions) < 1:
        task.complete = True
        task.update_status("ERROR", "There are no acquistions for this parameter set.")
        return None

    # the actual acquisitino exists, lets try the baseline:
    validation_params = {**parameters}
    # there were no acquisitions in the year 1000, hopefully
    validation_params.update({
        'time': (task.time_start.replace(year=task.time_start.year - 5), task.time_start - timedelta(microseconds=1))
    })
    acquisitions = dc.list_acquisition_dates(**validation_params)

    # list/map/int chain required to cast int to each baseline month, it won't work if they're strings.
    grouped_dates = group_datetimes_by_month(acquisitions, months=list(map(int, task.baseline_selection.split(","))))

    if not grouped_dates:
        task.complete = True
        task.update_status("ERROR", "There are no acquistions for this parameter set.")
        return None
    task.update_status("WAIT", "Validated parameters.")

    if not dc.validate_measurements(parameters['product'], parameters['measurements']):
        parameters['measurements'] = ['blue', 'green', 'red', 'nir', 'swir1', 'swir2', 'pixel_qa']

    dc.close()
    return parameters


@task(name="ndvi_anomaly.perform_task_chunking", base=BaseTask)
def perform_task_chunking(parameters, task_id=None):
    """Chunk parameter sets into more manageable sizes

    Uses functions provided by the task model to create a group of
    parameter sets that make up the arg.

    Args:
        parameters: parameter stream containing all kwargs to load data

    Returns:
        parameters with a list of geographic and time ranges

    """

    if parameters is None:
        return None

    task = NdviAnomalyTask.objects.get(pk=task_id)
    dc = DataAccessApi(config=task.config_path)
    dates = dc.list_acquisition_dates(**parameters)
    task_chunk_sizing = task.get_chunk_size()

    geographic_chunks = create_geographic_chunks(
        longitude=parameters['longitude'],
        latitude=parameters['latitude'],
        geographic_chunk_size=task_chunk_sizing['geographic'])

    grouped_dates_params = {**parameters}
    grouped_dates_params.update({'time': (datetime(1000, 1, 1), task.time_start - timedelta(microseconds=1))})
    acquisitions = dc.list_acquisition_dates(**grouped_dates_params)
    grouped_dates = group_datetimes_by_month(acquisitions, months=list(map(int, task.baseline_selection.split(","))))
    # create a single monolithic list of all acq. dates - there should be only one.
    time_chunks = []
    for date_group in grouped_dates:
        time_chunks.extend(grouped_dates[date_group])
    # time chunks casted to a list, essnetially.
    time_chunks = [time_chunks]

    logger.info("Time chunks: {}, Geo chunks: {}".format(len(time_chunks), len(geographic_chunks)))

    dc.close()
    task.update_status("WAIT", "Chunked parameter set.")

    return {'parameters': parameters, 'geographic_chunks': geographic_chunks, 'time_chunks': time_chunks}


@task(name="ndvi_anomaly.start_chunk_processing", base=BaseTask)
def start_chunk_processing(chunk_details, task_id=None):
    """Create a fully asyncrhonous processing pipeline from paramters and a list of chunks.

    The most efficient way to do this is to create a group of time chunks for each geographic chunk,
    recombine over the time index, then combine geographic last.
    If we create an animation, this needs to be reversed - e.g. group of geographic for each time,
    recombine over geographic, then recombine time last.

    The full processing pipeline is completed, then the create_output_products task is triggered, completing the task.

    """

    if chunk_details is None:
        return None

    parameters = chunk_details.get('parameters')
    geographic_chunks = chunk_details.get('geographic_chunks')
    time_chunks = chunk_details.get('time_chunks')

    assert len(time_chunks) == 1, "There should only be one time chunk for NDVI anomaly operations."

    task = NdviAnomalyTask.objects.get(pk=task_id)
    task.total_scenes = len(geographic_chunks) * len(time_chunks) * (task.get_chunk_size()['time']
                                                                     if task.get_chunk_size()['time'] is not None else
                                                                     len(time_chunks[0]))
    task.scenes_processed = 0
    task.update_status("WAIT", "Starting processing.")

    logger.info("START_CHUNK_PROCESSING")

    processing_pipeline = group([
        group([
            processing_task.s(
                task_id=task_id,
                geo_chunk_id=geo_index,
                time_chunk_id=time_index,
                geographic_chunk=geographic_chunk,
                time_chunk=time_chunk,
                **parameters) for time_index, time_chunk in enumerate(time_chunks)
        ]) for geo_index, geographic_chunk in enumerate(geographic_chunks)
    ]) | recombine_geographic_chunks.s(task_id=task_id)

    processing_pipeline = (processing_pipeline | create_output_products.s(task_id=task_id)).apply_async()
    return True


@task(name="ndvi_anomaly.processing_task", acks_late=True, base=BaseTask)
def processing_task(task_id=None,
                    geo_chunk_id=None,
                    time_chunk_id=None,
                    geographic_chunk=None,
                    time_chunk=None,
                    **parameters):
    """Process a parameter set and save the results to disk.

    Uses the geographic and time chunk id to identify output products.
    **params is updated with time and geographic ranges then used to load data.
    the task model holds the iterative property that signifies whether the algorithm
    is iterative or if all data needs to be loaded at once.

    Args:
        task_id, geo_chunk_id, time_chunk_id: identification for the main task and what chunk this is processing
        geographic_chunk: range of latitude and longitude to load - dict with keys latitude, longitude
        time_chunk: list of acquisition dates
        parameters: all required kwargs to load data.

    Returns:
        path to the output product, metadata dict, and a dict containing the geo/time ids
    """

    chunk_id = "_".join([str(geo_chunk_id), str(time_chunk_id)])
    task = NdviAnomalyTask.objects.get(pk=task_id)

    logger.info("Starting chunk: " + chunk_id)
    if not os.path.exists(task.get_temp_path()):
        return None

    metadata = {}

    def _get_datetime_range_containing(*time_ranges):
        return (min(time_ranges) - timedelta(microseconds=1), max(time_ranges) + timedelta(microseconds=1))

    base_scene_time_range = parameters['time']

    dc = DataAccessApi(config=task.config_path)
    updated_params = parameters
    updated_params.update(geographic_chunk)

    # Generate the baseline data - one time slice at a time
    full_dataset = []
    for time_index, time in enumerate(time_chunk):
        updated_params.update({'time': _get_datetime_range_containing(time)})
        data = dc.get_dataset_by_extent(**updated_params)
        if data is None or 'time' not in data:
            logger.info("Invalid chunk.")
            continue
        full_dataset.append(data.copy(deep=True))

    # load selected scene and mosaic just in case we got two scenes (handles scene boundaries/overlapping data)
    updated_params.update({'time': base_scene_time_range})
    selected_scene = dc.get_dataset_by_extent(**updated_params)

    if len(full_dataset) == 0 or 'time' not in selected_scene:
        return None

    #concat individual slices over time, compute metadata + mosaic
    baseline_data = xr.concat(full_dataset, 'time')
    baseline_clear_mask = create_cfmask_clean_mask(
        baseline_data.cf_mask) if 'cf_mask' in baseline_data else create_bit_mask(baseline_data.pixel_qa, [1, 2])
    metadata = task.metadata_from_dataset(metadata, baseline_data, baseline_clear_mask, parameters)

    selected_scene_clear_mask = create_cfmask_clean_mask(
        selected_scene.cf_mask) if 'cf_mask' in selected_scene else create_bit_mask(selected_scene.pixel_qa, [1, 2])
    metadata = task.metadata_from_dataset(metadata, selected_scene, selected_scene_clear_mask, parameters)
    selected_scene = task.get_processing_method()(selected_scene,
                                                  clean_mask=selected_scene_clear_mask,
                                                  intermediate_product=None)
    # we need to re generate the clear mask using the mosaic now.
    selected_scene_clear_mask = create_cfmask_clean_mask(
        selected_scene.cf_mask) if 'cf_mask' in selected_scene else create_bit_mask(selected_scene.pixel_qa, [1, 2])

    ndvi_products = compute_ndvi_anomaly(
        baseline_data,
        selected_scene,
        baseline_clear_mask=baseline_clear_mask,
        selected_scene_clear_mask=selected_scene_clear_mask)

    full_product = xr.merge([ndvi_products, selected_scene])

    task.scenes_processed = F('scenes_processed') + 1
    task.save()

    path = os.path.join(task.get_temp_path(), chunk_id + ".nc")
    full_product.to_netcdf(path)
    dc.close()
    logger.info("Done with chunk: " + chunk_id)
    return path, metadata, {'geo_chunk_id': geo_chunk_id, 'time_chunk_id': time_chunk_id}


@task(name="ndvi_anomaly.recombine_geographic_chunks", base=BaseTask)
def recombine_geographic_chunks(chunks, task_id=None):
    """Recombine processed data over the geographic indices

    For each geographic chunk process spawned by the main task, open the resulting dataset
    and combine it into a single dataset. Combine metadata as well, writing to disk.

    Args:
        chunks: list of the return from the processing_task function - path, metadata, and {chunk ids}

    Returns:
        path to the output product, metadata dict, and a dict containing the geo/time ids
    """
    logger.info("RECOMBINE_GEO")
    total_chunks = [chunks] if not isinstance(chunks, list) else chunks
    total_chunks = [chunk for chunk in total_chunks if chunk is not None]
    geo_chunk_id = total_chunks[0][2]['geo_chunk_id']
    time_chunk_id = total_chunks[0][2]['time_chunk_id']

    metadata = {}
    task = NdviAnomalyTask.objects.get(pk=task_id)

    chunk_data = []

    for index, chunk in enumerate(total_chunks):
        metadata = task.combine_metadata(metadata, chunk[1])
        chunk_data.append(xr.open_dataset(chunk[0], autoclose=True))

    combined_data = combine_geographic_chunks(chunk_data)

    path = os.path.join(task.get_temp_path(), "recombined_geo_{}.nc".format(time_chunk_id))
    combined_data.to_netcdf(path)
    logger.info("Done combining geographic chunks for time: " + str(time_chunk_id))
    return path, metadata, {'geo_chunk_id': geo_chunk_id, 'time_chunk_id': time_chunk_id}


@task(name="ndvi_anomaly.create_output_products", base=BaseTask)
def create_output_products(data, task_id=None):
    """Create the final output products for this algorithm.

    Open the final dataset and metadata and generate all remaining metadata.
    Convert and write the dataset to variuos formats and register all values in the task model
    Update status and exit.

    Args:
        data: tuple in the format of processing_task function - path, metadata, and {chunk ids}

    """
    logger.info("CREATE_OUTPUT")
    full_metadata = data[1]
    dataset = xr.open_dataset(data[0], autoclose=True)
    task = NdviAnomalyTask.objects.get(pk=task_id)

    task.result_path = os.path.join(task.get_result_path(), "ndvi_difference.png")
    task.scene_ndvi_path = os.path.join(task.get_result_path(), "scene_ndvi.png")
    task.baseline_ndvi_path = os.path.join(task.get_result_path(), "baseline_ndvi.png")
    task.ndvi_percentage_change_path = os.path.join(task.get_result_path(), "ndvi_percentage_change.png")
    task.result_mosaic_path = os.path.join(task.get_result_path(), "result_mosaic.png")
    task.data_path = os.path.join(task.get_result_path(), "data_tif.tif")
    task.data_netcdf_path = os.path.join(task.get_result_path(), "data_netcdf.nc")
    task.final_metadata_from_dataset(dataset)
    task.metadata_from_dict(full_metadata)

    bands = [
        'blue', 'green', 'red', 'nir', 'swir1', 'swir2', 'cf_mask', 'scene_ndvi', 'baseline_ndvi', 'ndvi_difference',
        'ndvi_percentage_change'
    ] if 'cf_mask' in dataset else [
        'blue', 'green', 'red', 'nir', 'swir1', 'swir2', 'pixel_qa', 'scene_ndvi', 'baseline_ndvi', 'ndvi_difference',
        'ndvi_percentage_change'
    ]

    dataset.to_netcdf(task.data_netcdf_path)

    write_geotiff_from_xr(task.data_path, dataset.astype('float64'), bands=bands)
    write_single_band_png_from_xr(
        task.result_path, dataset, 'ndvi_difference', color_scale=task.color_scales['ndvi_difference'])
    write_single_band_png_from_xr(
        task.ndvi_percentage_change_path,
        dataset,
        'ndvi_percentage_change',
        color_scale=task.color_scales['ndvi_percentage_change'])
    write_single_band_png_from_xr(
        task.scene_ndvi_path, dataset, 'scene_ndvi', color_scale=task.color_scales['scene_ndvi'])
    write_single_band_png_from_xr(
        task.baseline_ndvi_path, dataset, 'baseline_ndvi', color_scale=task.color_scales['baseline_ndvi'])

    write_png_from_xr(task.result_mosaic_path, dataset, bands=['red', 'green', 'blue'], scale=(0, 4096))

    dates = list(map(lambda x: datetime.strptime(x, "%m/%d/%Y"), task._get_field_as_list('acquisition_list')))
    if len(dates) > 1:
        task.plot_path = os.path.join(task.get_result_path(), "plot_path.png")
        create_2d_plot(
            task.plot_path,
            dates=dates,
            datasets=task._get_field_as_list('clean_pixel_percentages_per_acquisition'),
            data_labels="Clean Pixel Percentage (%)",
            titles="Clean Pixel Percentage Per Acquisition")

    logger.info("All products created.")
    # task.update_bounds_from_dataset(dataset)
    task.complete = True
    task.execution_end = datetime.now()
    task.update_status("OK", "All products have been generated. Your result will be loaded on the map.")
    shutil.rmtree(task.get_temp_path())
    return True
