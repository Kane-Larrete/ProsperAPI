"""crest_utils.py collection of tools for handling crest endpoint APIs"""

from os import path, makedirs
from datetime import datetime
import pytz
import configparser

import ujson as json
import requests
from tinydb import TinyDB, Query
import pandas as pd
from pandas.io.json import json_normalize

requests.models.json = json

import publicAPI.exceptions as exceptions
import publicAPI.config as api_config
import prosper.common.prosper_logging as p_logging

LOGGER = p_logging.DEFAULT_LOGGER
HERE = path.abspath(path.dirname(__file__))

CACHE_PATH = path.join(HERE, 'cache')
makedirs(CACHE_PATH, exist_ok=True)

def setup_cache_file(
        cache_filename,
        cache_path=CACHE_PATH,
):
    """build tinyDB handle to cache file

    Args:
        cache_filename (str): path to desired cache file
        cache_path (str, optional): path to cache folder
        logger (:obj:`logging.logger`, optional): logging handle

    Returns:
        (:obj:`TinyDB.TinyDB`): cache db

    """
    db_filename = path.join(cache_path, cache_filename + '.json')
    return TinyDB(db_filename)

def write_cache_entry(
        tinydb_handle,
        index_key,
        data_payload
):
    """write data to tinydb

    Args:
        tinydb_handle (:obj:`tinydb.TinyDB`): database to write to
        index_key (any, int): index key value
        data_payload (:obj:`dict`): object to save to cache

    Returns:
        None

    """
    if tinydb_handle.search(Query().index_key == index_key):
        tinydb_handle.update(
            {
                'cache_datetime': datetime.utcnow().timestamp(),
                'payload': data_payload
            },
            Query().index_key == index_key
        )
    else:
        tinydb_handle.insert(
            {
                'cache_datetime': datetime.utcnow().timestamp(),
                'payload': data_payload,
                'index_key': index_key

            }
        )

def endpoint_to_kwarg(
        endpoint_name,
        type_id
):
    """recast endpoint_name to **kwarg pair

    TODO: this is kinda hacky, move to class/enum?
    Args:
        endpoint_name (str): name of endpoint
        type_id (int): EVE Online ID

    Returns:
        (:obj:`dict`) kwarg pair

    """
    kwarg_pair = {}
    if endpoint_name == 'inventory_types':
        kwarg_pair = {'type_id': type_id}
    elif endpoint_name =='map_regions':
        kwarg_pair = {'region_id': type_id}
    else:
        raise exceptions.UnsupportedCrestEndpoint(
            'No configuration for ' + str(endpoint_name)
        )

    return kwarg_pair

def validate_id(
        endpoint_name,
        type_id,
        cache_buster=False,
        config=api_config.CONFIG,
        logger=LOGGER
):
    """Check EVE Online CREST as source-of-truth for id lookup

    Args:
        endpoint_name (str): desired endpoint for data lookup
        type_id (int): id value to look up at endpoint (NOTE: only SDE replacement)
        cache_buster (bool, optional): skip caching, fetch from internet
        logger (:obj:`logging.logger`, optional): logging handle

    Returns:
        (int): HTTP status code for error validation

    """
    ## Check local cache for value ##
    if not cache_buster:
        logger.info('--searching cache for type_id: {0}'.format(type_id))
        logger.debug('endpoint_name={0}'.format(endpoint_name))
        logger.debug('type_id={0}'.format(type_id))

        try:
            db_handle = setup_cache_file(endpoint_name)
            cache_time = datetime.utcnow().timestamp() - \
                int(config.get('CACHING', 'sde_cache_limit'))
            cache_val = db_handle.find(
                Query().cache_datetime >= cache_time &
                Query().index_key == type_id
            )
        except Exception as err_msg:
            logger.error(
                'ERROR: unable to connect to local tinyDB cache' +
                '\n\tendpoint_name: {0}'.format(endpoint_name) +
                '\n\tcache_path: {0}'.format(CACHE_PATH),
                exc_info=True
            )
            pass #OK if no cache, continue

        if cache_val:
            logger.info('--found type_id cache for type_id: {0}'.format(type_id))
            logger.debug(cache_val)
            return cache_val    #skip CREST

    ## Request info from CREST ##
    try:
        logger.info('--fetching CREST ID information')
        logger.debug('endpoint_name={0}'.format(endpoint_name))
        logger.debug('type_id={0}'.format(type_id))
        kwarg_pair = endpoint_to_kwarg(
            endpoint_name,
            type_id
        )
        type_info = fetch_crest_endpoint(
            endpoint_name,
            **kwarg_pair
            #TODO: index_key to key/val pair
        )
    except Exception as err_msg:
        logger.error(
            'ERROR: unable to connect to CREST' +
            '\n\tendpoint_name: {0}'.format(endpoint_name) +
            '\n\ttype_id: {0}'.format(type_id),
            exc_info=True
        )
        raise exceptions.IDValidationError(
            status=404,
            message='Unable to validate {0}:{1}'.format(
                endpoint_name,
                type_id
            )
        )

    ## Update cache ##
    logger.info('--updating cache')
    try:
        write_cache_entry(
            db_handle,
            type_id,
            type_info
        )
    except Exception as err_msg:
        logger.error(
            'ERROR: unable to write to cache',
            exc_info=True
        )

        pass

    return type_info

CREST_BASE = 'https://crest-tq.eveonline.com/'
def fetch_crest_endpoint(
        endpoint_name,
        crest_base=CREST_BASE,
        config=api_config.CONFIG,
        **kwargs
):
    """Fetch payload from EVE Online's CREST service

    Args:
        endpoint_name (str): name of endpoint (in config)
        config (`configparser.ConfigParser`, optional): override for config obj
        **kwargs (:obj:`dict`): key/values to overwrite in query

    Returns:
        (:obj:`dict`): JSON object returned by endpoint

    """
    try:
        crest_url = crest_base + config.get('RESOURCES', endpoint_name)
    except configparser.NoOptionError:
        raise exceptions.UnsupportedCrestEndpoint(
            'No {0} found in [RESOURCES]'.format(endpoint_name))

    try:
        crest_url = crest_url.format(**kwargs)
    except KeyError as err_msg:
        raise exceptions.CrestAddressError(repr(err_msg))

    headers = {
        'User-Agent': config.get('GLOBAL', 'useragent')
    }

    # no try-except, catch in caller
    # done to make logging path easier
    req = requests.get(
        crest_url,
        headers=headers
    )
    req.raise_for_status()
    data = req.json()

    return data

def fetch_market_history(
        region_id,
        type_id,
        logger=LOGGER
):
    """Get market history data from EVE Online CREST endpoint

    Args:
        region_id (int): (validated) regionID value for CREST lookup
        type_id (int): (validated) typeID value for CREST lookup
        logger (:obj:`logging.logger`, optional): logging handle

    Returns:
        (:obj:`pandas.data_frame`) pandas collection of data
            ['date', 'avgPrice', 'highPrice', 'lowPrice', 'volume', 'orders']
    """
    pass

def OHLC_to_format(
        data,
        format_type,
        logger=LOGGER
):
    """reformat pandas dataframe to desired format (and recast keys if required)

    Args:
        data (:obj:`pandas.data_frame`): data to format for release
        format_type(:enum:`AcceptedDataFormat`): desired data type
        logger (:obj:`logging.logger`, optional): logging handle

    Returns:
        (`list` or `dict`) processed output

    """
    pass
