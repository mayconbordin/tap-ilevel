from datetime import datetime, timedelta
import singer
from singer import metrics, metadata, Transformer, utils
from singer.utils import strptime_to_utc, strftime
from tap_ilevel.transform import transform_json
#from tap_ilevel.streams import STREAMS
from dateutil.relativedelta import *
#import attr
import json
from tap_ilevel.streams import STREAMS
from pytz import timezone
import pytz
import time
from singer import metrics
import dateutil.parser
from .request_state import RequestState
from .iget_formula import IGetFormula

LOGGER = singer.get_logger()

MAX_ID_CHUNK_SIZE = 20000 # Requests to retrieve object details are restricted by a limit

"""
 Publish schema to singer
"""
def __write_schema(catalog, stream_name):
    stream = catalog.get_stream(stream_name)
    schema = stream.schema.to_dict()
    try:
        singer.write_schema(stream_name, schema, stream.key_properties)
    except OSError as err:
        LOGGER.info('OS Error writing schema for: %s', stream_name)
        raise err
"""
 Publish individual record...
"""
def __write_record(stream_name, record, time_extracted):
    LOGGER.debug('Attempting to write rercord: type: %s', stream_name)
    try:
        singer.messages.write_record(stream_name, record, time_extracted=time_extracted)
    except OSError as err:
        LOGGER.info('OS Error writing record for: %s', stream_name)
        LOGGER.info('record: %s', record)
        LOGGER.info(err)
        raise err
    except TypeError as err:
        LOGGER.info('Type Error writing record for: %s', stream_name)
        LOGGER.info('record: %s', record)
        raise err

def __get_bookmark(state, stream, default):
    """Retrieve current bookmark."""
    if (state is None) or ('bookmarks' not in state):
        return default
    return (
        state.get('bookmarks', {}).get(stream, default)
    )

def __write_bookmark(state, stream, value):
    """Set current bookmark."""
    if 'bookmarks' not in state:
        state['bookmarks'] = {}
    state['bookmarks'][stream] = value
    singer.write_state(state)

"""
 Sync a specific endpoint (stream)

    According to the documentation: "The Web Services methods can be broken up into six
    categories" (API Call Descriptions section)
        • Entities (Assets, Funds, Securities, Scenarios)
        • Entity Relationships
        • Data Items
        • Cash Transactions (Transactions, etc..)
        • Currency Rates
        • Documents (Currently not importing)

    The retrieval methods for each data source (stream) will be dependant on the object type.
    Data sources/retrieval strategies are as follows. Generally speaking, data retrieval
    methods are fall into two categories; complete table refreshes, or deltas.
    
"""
def __sync_endpoint(client,
                  catalog,
                  state,
                  endpoint_config,
                  start_date,
                  stream,
                  path,
                  static_params,
                  selected_streams=None,
                  base_url=None,
                  bookmark_field=None,
                  bookmark_query_field=None,
                  bookmark_type=None):

    # Top level variables
    endpoint_total = 0
    total_records = 0
    stream_name = stream.stream
    data_key = endpoint_config.get('data_key')
    start = time.time()

    LOGGER.info('syncing stream : %s', stream_name)
    __update_currently_syncing(state, stream_name)

    #Define window period for data load
    start_dt = __get_start_date(stream_name, endpoint_config.get('bookmark_type'), state, start_date)
    end_dt = __get_end_date()

    #Establish bookmark values
    bookmark_type = endpoint_config.get('bookmark_type')
    bookmark_field = next(iter(endpoint_config.get('replication_keys', [])), None)

    # Publish schema to singer
    __write_schema(catalog, stream_name)
    LOGGER.info('Processing date window for stream %s, %s to %s', stream_name, start_dt, end_dt)

    #Define entity types that use a common API for retrieval
    entity_api_streams = [''] #Add data items

    # Delegate processing to the appropriate routine: Certain entities may be retrieved from a
    # common API call, while others are obtained by alternate sources.
    if(stream_name=="investment_transactions"):
        endpoint_total = __process_investment_transactions_stream(client, stream, data_key, start_dt, end_dt, state, bookmark_field)
    elif (stream_name=="asset_periodic_data"):
        with metrics.record_counter('assets') as counter:
            req_state = __get_request_state(client, stream_name, data_key, start_dt, end_dt, state, 'last_modified_date', stream)
            # All assets are retrieved as alternate call accepting date criteria provides a subset of data
            with metrics.record_counter('assets') as counter:
                assets_response = client.service.GetAssets()
                assets = assets_response[0]
                endpoint_total = __process_periodic_data(assets, req_state)
    elif (stream_name == "fund_periodic_data"):
        with metrics.record_counter('funds') as counter:
            # All assets are retrieved as alternate call accepting date criteria provides a subset of data
            funds_response = client.service.GetFunds()
            funds = funds_response[0]
            endpoint_total = __process_periodic_data(client, stream, data_key, start_dt, end_dt, state)
    elif (stream_name in entity_api_streams):
        endpoint_total =  __process_object_stream_type(endpoint_config, state, stream, start_dt, end_dt, client,
                               catalog, bookmark_field)
    else:
        endpoint_total = __perform_api_call(client, stream, data_key, start_dt, bookmark_field, state)

    end = time.time()
    elapsed_time = end - start
    LOGGER.info('Processed a total of %s records for stream %s in %s', endpoint_total, stream_name, elapsed_time)

    return endpoint_total

def __get_request_state(client, stream_name, data_key, start_dt, end_dt, state, bookmark_field, stream):
    req_state = RequestState()
    req_state.client = client
    req_state.stream_name = stream_name
    req_state.data_key = data_key
    req_state.start_dt = start_dt
    req_state.end_dt = end_dt
    req_state.state = state
    req_state.bookmark_field = bookmark_field
    req_state.stream = stream
    return req_state

def __perform_api_call(client, stream, data_key, last_bookmark_date, bookmark_field, state):
    """Retrieve object references by stream name."""
    updated_record_count = 0
    stream_name = stream.stream
    schema = stream.schema.to_dict()
    records = None

    top_level_entities = ["assets", "funds"]
    last_bookmark_date_dt = __convert_iso_8601_date(last_bookmark_date)

    try: #TODO: Add metrics...
        LOGGER.info('Loading assets')
        if stream_name == 'assets':
            result = client.service.GetAssets()
        elif stream_name == 'funds':
            result = client.service.GetFunds()
        elif stream_name == 'data_items':
            result = client.service.GetDataItems()
        elif stream_name == 'scenarios':
            result = client.service.GetScenarios()
        elif stream_name == 'securities':
            result = client.service.GetSecurities()
        elif stream_name == 'object_relations':
            result = client.service.GetObjectRelationships()
        elif stream_name == 'investments':
            result = client.service.GetInvestments()

        records = result[0]
        total_record_count = len(records)
        LOGGER.info('Preparing to process a max total of %s objects', total_record_count)
        updated_ids = process_stream(records, stream, bookmark_field, data_key, last_bookmark_date_dt, state)
        LOGGER.info('Total number of records processed in stream %s was %s', stream_name, len(updated_ids))

        return total_record_count
    except Exception as err:
        err_msg = 'API call failed: {}, for type: {}'.format(err, stream_name)
        LOGGER.error(err_msg)

"""
 Certain entities (Funds, Assets) are returned via a common API call, which has a limitation on 
 the duration of the overall specified date range. This method will 'chunk' the given date 
 range (if required) into batches that fall within the limit defined by the API call. Actual 
 processing is delegated to other methods. 
"""
def __process_object_stream_type(endpoint_config, state, stream, start_dt, end_dt, client,
                               catalog, bookmark_field):

    stream_name = stream.stream
    endpoint_total = 0
    # Initialization for date related operations:
    # Operations must be performed in 30 day (max) increments
    date_chunks = __get_date_chunks(start_dt, end_dt, 30)
    LOGGER.info('Total number of date periods to process: ' + str(len(date_chunks)))
    cur_start_date = date_chunks[0]
    date_chunks.pop(0)
    cur_end_date = date_chunks[0]
    date_chunks.pop(0)
    cur_date_range_index = 1
    cur_date_criteria_length = len(date_chunks)

    if cur_start_date == cur_end_date:
        LOGGER.info('Last bookmark matches current date, no processing required')
        return 0

    # Main loop: Process records by date chunks
    with metrics.record_counter(stream_name) as counter:
        for cur_date_criteria_index in range(cur_date_criteria_length):
            cur_date_criteria = date_chunks[cur_date_criteria_index]
            LOGGER.info('processing date range: ' + str(cur_start_date) + "' '" +
                        str(cur_end_date) + "', " + str(
                            cur_date_range_index) + " of " + str(cur_date_criteria_length))

            endpoint_total = endpoint_total + __process_date_range(stream, cur_start_date,
                                                                 cur_end_date, client, catalog,
                                                                 endpoint_config, bookmark_field)

    return endpoint_total

"""
 Process records for a given date range for streams that use a common API call to produce data 
 (Funds, Assets, etc.)

 The GetObjectsByIds(...) API calls used to retrieve certain data types enforce limitations 
 on the date range supplied as a parameter (30 days). This method expects the specified 
 date range to fall within the date limit.

 Records are retrieved (in chunks if required), converted, and produced.
"""
def __process_date_range(stream, cur_start_date, cur_end_date, client, catalog, endpoint_config,
                       bookmark_field):
    update_count = 0
    stream_name = stream.stream
    # Retrieve ids for updated/inserts objects for given date range
    try:
        # Required to establish access to a SOAP alias for a given object type
        object_type = client.factory.create('tns:UpdatedObjectTypes')
        # Make call to retrieve updated objects for max 30 day date range (object details to be
        # retrieved in additional call)
        updated_asset_ids_all = client.service.GetUpdatedObjects(__get_asset_ref(object_type,
                                                                               stream_name),
                                                                 cur_start_date,
                                                                 cur_end_date)
        LOGGER.info('Successfully retrieved ids for recently created/updated objects')
        updated_result_count = len(updated_asset_ids_all)

        # Determine if there are any records to be processed, and if so there is a 20k limitation
        # for retrieving details by ids.
        if updated_result_count == 0:
            LOGGER.info('No inserts/updates available for stream %s', stream_name)
            return 0

        LOGGER.info('Processing %s updated records', updated_result_count)


        id_sets = __split_id_set(updated_asset_ids_all, MAX_ID_CHUNK_SIZE)
        LOGGER.info('Total number of id set to process is %s', len(updated_asset_ids_all))

        # Outer loop: Iterate through each set of object ids to process (within API limits)
        id_set_count = 1

        for cur_id_set in id_sets:
            LOGGER.info('Processing id set %s of %s ', id_set_count, id_sets)

            # Result of id chunking operation will return array based data structure,
            # first we need to convert
            # to data type expected by API
            array_of_int = client.factory.create('ns3:ArrayOfint')
            array_of_int.int = cur_id_set

            # Perform update operations
            data_key = endpoint_config.get('data_key', 'data')
            update_count = update_count + __process_object_set(stream, array_of_int, False,
                                                             client, catalog,
                                                             endpoint_config,
                                                             bookmark_field, data_key)

            id_set_count = id_set_count + 1
    except Exception as err:
        err_msg = 'error: {}, for type: {}'.format(err, stream_name)
        LOGGER.error(err_msg)
        return 0

    return update_count



def format_date_simple(date_ref):
    date_ref = date_ref.strftime("%Y-%m-%d")
    return datetime.strptime(date_ref, "%Y-%m-%d")



def __get_ids_as_array_of_int(ids, client):
    entity_ids_array = []

    for id in ids:
        entity_ids_array.append(id)

    entity_ids_array = client.factory.create('ns3:ArrayOfint')
    entity_ids_array.int = entity_ids_array
    return entity_ids_array
"""
 Sync data for certain entity types (Assets,Funds, etc...) that are retrieved from an API 
 call that produces ids. Given a set of ids, the corresponding object details are retrieved, 
 and then processed. Note: API used to retrieve object details imposes a limit on the 
 number of records that may be submitted, so this method expects the IDs to be under 
 that limit. 
"""
def __process_object_set(stream, object_ids, is_deleted_refs, client, catalog, endpoint_config,
                       bookmark_field, data_key):

    object_type = client.factory.create('tns:UpdatedObjectTypes')
    stream_name = stream.stream
    call_response = client.service.GetObjectsByIds(__get_asset_ref(object_type, stream_name),
                                                   object_ids)

    stream = catalog.get_stream(stream_name)

    total_update_count = 0
    object_refs = []

    total_record_count = len(object_refs)
    cur_record_count = 1

    records = call_response.NamedEntity

    for record in records:
        LOGGER.info('Processing record '+ str(cur_record_count) +' of '+
                    str(total_record_count) +' total')
        try:
            transformed_record = __transform_record(record, stream, data_key)

            __write_record(stream_name, transformed_record, utils.now())

            total_update_count = total_update_count + 1
            LOGGER.info('Updating record count: %s', total_update_count)

        except Exception as err:
            err_msg = 'Error during transformation for entity: {}, for type: {}, obj: {}'\
                .format(err, stream_name, transformed_record)
            LOGGER.error(err_msg)

        cur_record_count = cur_record_count + 1

    LOGGER.info('process_object_set: total record count is %s ', total_update_count)
    return total_update_count


"""
 Make data more 'database compliant', i.e. rename columns, convert to UTC timezones, etc. 
 'transform_data' method needs to ensure raw data matches that in the schemas....
"""
def __transform_record(record, stream, data_key):

    obj_dict = __obj_to_dict(record) #Convert SOAP object to dict
    object_json_str = json.dumps(obj_dict, indent=4, sort_keys=True, default=str)  # Object dict converted to JSON string
    object_json_str = object_json_str.replace('True','true')
    object_json = json.loads(object_json_str) #Parse JSON

    stream_metadata = metadata.to_map(stream.metadata)
    transformed_data = transform_json(object_json)

    # singer validation check
    with Transformer() as transformer:
        transformed_record = transformer.transform(
            transformed_data,
            stream.schema.to_dict(),
            stream_metadata)

    return transformed_data




"""
 When calls are performed to retrieve object details by id, we are restricted by a 20k limit, so 
 we need to support the ability to split a given set into chunks of a given size. Note, we are 
 accepting a SOAP data type (ArrayOfInts) and returning an array of arrays which will need to 
 be converted prior to submission to any additional SOAP calls.
"""
def __split_id_set(ids, max_len):
    result = []
    if len(ids) < max_len:
        cur_id_set = []
        for id in ids:
            cur_id_set.append(id)
        result.append(cur_id_set)
        return result

    chunk_count = len(ids) // max_len
    remaining_records = len(ids) % max_len

    cur_chunk_index = 0
    total_index = 0
    source_index = 0
    while cur_chunk_index < chunk_count:
        cur_id_set = []
        while source_index < max_len:
            cur_id_set.append(ids[total_index])
            total_index = total_index + 1
            source_index = source_index + 1
        result.append(cur_id_set)
        cur_chunk_index = cur_chunk_index + 1

    if remaining_records > 0:
        source_index = 0
        cur_id_set = []
        cur_chunk_index = cur_chunk_index + 1
        cur_index = 0
        source_index = 0
        while source_index < remaining_records:
            cur_id_set.append(ids[total_index])
            total_index = total_index + 1
            source_index = source_index + 1
        result.append(cur_id_set)

    return result

"""
 Certain API calls have a limitation of 30 day periods, where the process might be launched 
 with an overall activity window of a greater period of time. Date ranges sorted into 30 
 day chunks in preparation for processing.
 
 Values provided for input dates are in format rerquired by SOAP API (yyyy-mm-dd)

 API calls are performed within a maximum 30 day timeframe, so breaking a period of time 
 between two into limited 'chunks' is required
"""
def __get_date_chunks(start_date, end_date, max_days):

    td = timedelta(days=max_days)
    result = []

    days_dif = __get_num_days_diff(start_date, end_date)
    if days_dif < max_days:
        result.append(start_date)
        result.append(end_date)
        return result

    working = True
    cur_date = start_date
    result.append(cur_date)
    next_date = cur_date
    while working:

        next_date = (next_date + timedelta(days=max_days))
        if next_date == end_date or next_date > end_date:
            result.append(end_date)
            return result
        else:
            result.append(next_date)

    return result

"""
 Given stream name, identify the corresponding Soap identifier to send to the API. This is used 
 to identify the type of entity we are retrieving for certain API calls.
"""
def __get_asset_ref(attr, stream_ref):

    if stream_ref == 'assets':
        return attr.Asset
    elif stream_ref == 'currency_rates':
        return attr.CurrencyRate
    elif stream_ref == 'data_items':
        return attr.DataItem
    elif stream_ref == 'funds':
        return attr.Fund
    elif stream_ref == 'investment_transactions':
        return attr.InvestmentTransaction
    elif stream_ref == 'investments':
        return attr.Investment
    elif stream_ref == 'scenarios':
        return attr.Scenario
    elif stream_ref == 'securities':
        return attr.Security
    elif stream_ref == 'segments':
        return attr.SegmentNode
    return None

"""
 Currently syncing sets the stream currently being delivered in the state.
 If the integration is interrupted, this state property is used to identify
  the starting point to continue from.
 Reference: https://github.com/singer-io/singer-python/blob/master/singer/bookmarks.py#L41-L46
"""
def __update_currently_syncing(state, stream_name):
    LOGGER.info('Updating status of current stream processing')

    if (stream_name is None) and ('currently_syncing' in state):
        del state['currently_syncing']
    else:
        singer.set_currently_syncing(state, stream_name)
    singer.write_state(state)

"""
    Main routine: orchestrates pulling data for selected streams. 
"""
def sync(client, config, catalog, state, base_url):
    LOGGER.info('sync.py: sync()')
    LOGGER.info('state:')

    # Start date may be overridden by command line params
    if 'start_date' in config:
        start_date = config['start_date']

    # Get selected_streams from catalog, based on state last_stream
    #   last_stream = Previous currently synced stream, if the load was interrupted
    last_stream = singer.get_currently_syncing(state)
    LOGGER.info('last/currently syncing stream: {}'.format(last_stream))
    selected_streams = []
    selected_streams_by_name = {}
    for stream in catalog.get_selected_streams(state):
        selected_streams.append(stream.stream)
        selected_streams_by_name[stream.stream] = stream
    LOGGER.info('selected_streams: {}'.format(selected_streams))

    if not selected_streams or selected_streams == []:
        return

    # Loop through endpoints in selected_streams
    for stream_name, endpoint_config in STREAMS.items():
        if stream_name in selected_streams:
            LOGGER.info('START Syncing: {}'.format(stream_name))
            stream = selected_streams_by_name[stream_name]

            path = endpoint_config.get('path', stream_name)
            bookmark_field = next(iter(endpoint_config.get('replication_keys', [])), None)
            bookmark_query_field = bookmark_query_field = endpoint_config.\
                get('bookmark_query_field')
            bookmark_type = endpoint_config.get('bookmark_type')
            __write_schema(catalog, stream_name)
            total_records = 1

            # Main sync routine
            total_records = __sync_endpoint(
                client=client,
                catalog=catalog,
                state=state,
                endpoint_config=endpoint_config,
                start_date=start_date,
                stream=stream,
                path=path,
                static_params=endpoint_config.get('params', {}),
                selected_streams=selected_streams,
                base_url=base_url,
                bookmark_field=bookmark_field,
                bookmark_query_field=None,
                bookmark_type=None
            )

            __update_currently_syncing(state, None)
            LOGGER.info('FINISHED Syncing: {}, total_records: {}'.format(
                stream_name,
                total_records))

    LOGGER.info('sync.py: sync complete')


"""
 Provides ability to determine number of days between two given dates.
"""
def __get_num_days_diff(start_date, end_date):
    return abs((start_date - end_date).days)

def __process_investment_transactions_stream(client, stream, data_key, start_date, end_date, state, bookmark_field):
    """Retrieve investment transactions from a given point in time. Call operations for each date
    between the specified 'start' and 'end' dates will be individually be requested as this particular
    endpoint requires an 'exact match' on the criteria."""
    LOGGER.info('Processing date range %s to %s', start_date, end_date)

    date_criteria = __split_date_range_into_array(start_date, end_date)
    LOGGER.info('A total of %s requests for invest transactions will be required', len(date_criteria))
    result_count = 0
    with metrics.record_counter('investment transactions') as counter:
        for as_of_date in date_criteria:
            result_count = result_count +  __process_investment_transactions_for_as_of_date(client, stream, data_key, bookmark_field, as_of_date, state)
            __write_bookmark(state, stream.stream, __est_to_utc_datetime(as_of_date))

    return result_count

def __split_date_range_into_array(start_date, end_date):
    """Support the ability to create an array of individual days between two giiven dates"""
    result = []

    delta = end_date - start_date  # as timedelta

    for i in range(delta.days + 1):
        day = start_date + timedelta(days=i)
        result.append(day)

    return result

def __process_investment_transactions_for_as_of_date(client, stream, data_key, bookmark_field, start_date, state):
    """Retrieve investment transactions for a specific 'AsOfDate' criteria """
    updated_record_count = 0
    stream_name = stream.stream
    schema = stream.schema.to_dict()
    try:
        criteria = client.factory.create('InvestmentTransactionsSearchCriteria')
        target_date_str = datetime.strftime(start_date, '%Y-%m-%d')
        criteria.AsOfDate = target_date_str
        with metrics.record_counter('Retrieve investment for date') as counter:
            result = client.service.GetInvestmentTransactions(criteria)

        if isinstance(result, str):
                return 0

        records = result[0]
        updated_record_count = len(records)
        LOGGER.info('Preparing to process a total of %s investment transactions', updated_record_count)
        prev_date_ref = start_date - timedelta(1)

        updated_records = process_stream(records, stream, bookmark_field, data_key, prev_date_ref, state)
        updated_record_count = len(updated_records)
    except Exception as err:
        err_msg = 'API call failed: {}, for type: {}'.format(err, stream_name)
        LOGGER.error(err_msg)

    return updated_record_count

"""
 Date object returned from API call needs to be converted to format required by SingerIO 
"""
def __est_to_utc_datetime(date_val):
    date_str = date_val.strftime("%Y-%m-%d %H:%M:%S")
    timezone = pytz.timezone('US/Eastern')
    est_datetime = timezone.localize(datetime.strptime(
        date_str, "%Y-%m-%d %H:%M:%S"))
    utc_datetime = strftime(timezone.normalize(est_datetime).astimezone(
        pytz.utc))
    return utc_datetime

def __get_start_date(stream_name, bookmark_type, state, start_date):
    """Get start date for a given stream. For streams that are configured to use 'datetime' as a bookmarking
    strategy, the last known bookmark is used (if present). Otherwise, the default start date value is used
    if no bookmark may be located, or in cases where a full table refresh is appropriate."""

    if bookmark_type != 'datetime':
        return datetime.strptime(start_date, '%Y-%m-%dT%H:%M:%SZ')

    bookmark =  __get_bookmark(state, stream_name, start_date)

    if bookmark == None:
        return start_date

    return bookmark

def __get_end_date():
    """Obtain reference to end date used for tap processing window."""
    return datetime.now()

def process_stream(records, stream, bookmark_field, data_key, start_date, state):
    """Generic routine for processing objects."""
    LOGGER.info("processing object stream")

    stream_name = stream.stream
    processed_id_set = []

    max_bookmark_value = None
    i = 0
    with metrics.record_counter(stream_name) as counter:
        for record in records:

            try:
                transformed_record = __transform_record(record, stream, data_key)
                # If a bookmark is configured for the current stream, then ensure any records that have been added/deleted
                # have been past the specified start date. Whenever possible, we attempt to sync the minimal number of records
                # possible
                if bookmark_field != None:
                    LOGGER.info('Performing bookmark check')

                    # Filter through records to ensure that we are only publishing records that have been created/updated
                    cur_date_ref = __convert_iso_8601_date(transformed_record[bookmark_field])
                    if cur_date_ref<=start_date:
                        continue

                processed_id_set.append(transformed_record['id'])
                __write_record(stream_name, transformed_record, utils.now())

            except Exception as recordErr:
                err_msg = 'error during transformation for entity: {}, for type: {}, obj: {}' \
                    .format(recordErr, stream_name, transformed_record)
                LOGGER.error(recordErr)
                LOGGER.error(err_msg)

    return processed_id_set

def __convert_iso_8601_date(date_str):

    if isinstance(date_str, datetime):
        date_str = date_str.strftime("%Y-%m-%d")

    """Convert ISO 8601 formatted date string into time zone nieve"""
    cur_date_ref = dateutil.parser.parse(date_str)
    cur_date_ref = cur_date_ref.replace(tzinfo=None)
    return cur_date_ref

def transform_datetime(this_dttm):
    with Transformer() as transformer:
        new_dttm = transformer._transform_datetime(this_dttm)
    return new_dttm

def __get_entities(client, stream, data_key, last_bookmark_date, bookmark_field, state):
    updated_record_count = 0
    stream_name = stream.stream
    schema = stream.schema.to_dict()
    try:
        LOGGER.info('Loading assets')
        result = client.service.GetAssets()
        records = result[0]
        total_record_count = len(records)
        LOGGER.info('Preparing to process a total of %s records for stream %stream_name', total_record_count, stream_name)

        updated_record_count = process_stream(records, stream, bookmark_field, data_key, last_bookmark_date, state)

        LOGGER.info('Total number of records processed in stream %s was %s', stream_name, updated_record_count)
    except Exception as err:
        err_msg = 'API call failed: {}, for type: {}'.format(err, stream_name)
        LOGGER.error(err_msg)

    return updated_record_count


def __process_periodic_data(records, req_state):
    """Publish periodic data (reflections of updated attributes) for a given time period"""

    #Establish collection of ids for records in scope
    transformed_record_ids = __strip_record_ids(records)

    # Next a call to publish associated periodic data is performed.
    return __process_periodic_data_for_ids(transformed_record_ids, req_state)

def __strip_record_ids(records):
	ids = []
	for record in records:
		ids.append(record.Id)
	return ids

def __transform_records_for_bookmark_date(records, req_state):
    """Given a set of records, perform transformations in preparation for publishing. This method will
    accept a collection of records, and return a subset of those records that were updated after the
    specified bookmark date."""
    filtered_records = []

    for record in records:
        transformed_record = __transform_record(record, req_state.stream, req_state.data_key)
        cur_date_ref = __convert_iso_8601_date(transformed_record[req_state.bookmark_field])

        if cur_date_ref<=req_state.start_dt:
            continue

        filtered_records.append(transformed_record)

    return filtered_records

def __get_record_ids(transformed_records):
	"""Given a series of records, create an array id associated ids."""
	results = []

	for record in transformed_records:
		results.append(record.get('id'))

	return results

def __process_periodic_data_for_ids(ids, req_state):

    LOGGER.info('Processing periodic data for id set')

    update_count = 0

    #Break date/id sets into acceptable limits as required by API calls.
    end_date = datetime.now()
    date_chunks = __get_date_chunks(req_state.start_dt, req_state.end_dt, 30)

    cur_end_date = date_chunks[0]
    date_chunks.pop(0)
    cur_date_range_index = 0
    cur_date_criteria_length = len(date_chunks)
    id_set_chunks = __split_id_set(ids, MAX_ID_CHUNK_SIZE)
    LOGGER.info('Total number of id chunks for criteria is %s.', len(id_set_chunks))
    LOGGER.info('Total number of date for criteria is %s.', len(date_chunks))

    #Loop through date, and id 'chunks' as appropriate, processing each window.
    for cur_date_criteria_index in range(cur_date_criteria_length):
        cur_start_date = cur_end_date
        cur_end_date = date_chunks[cur_date_criteria_index]
        LOGGER.info('processing date range: %s - %s, index: %s of %s', cur_start_date, cur_end_date, cur_date_range_index,
                cur_date_criteria_length)
        for cur_id_set in id_set_chunks:
          update_count = update_count + __publish_iget_batch(cur_id_set, cur_start_date, cur_end_date, req_state)

    return update_count

def __publish_iget_batch(id_array, start_date, end_date, req_state):
    """Given a date window, and id set which meet the requirements of an API call, retrieve associated data and perform update...."""
    LOGGER.info('Processing date/id batch operation for date range %s-%s', start_date, end_date)

    update_count = 0

    entity_ids = req_state.client.factory.create('ns3:ArrayOfint')
    entity_ids.int = id_array

    #Perform API call to retrieve 'standardized ids' in preparation for next call
    with metrics.http_request_timer('Retrieve standardized ids') as timer:
        LOGGER.info('API call: Translating records from ids to standardized ids for %s records.', len(id_array))
        updated_data_ids = req_state.client.service.GetUpdatedData(start_date, end_date, entity_ids)
        LOGGER.info('Request time %s', timer.elapsed)

    #Validate that there is data to process
    if isinstance(updated_data_ids, str):
        return 0


    #Given resulting 'standardized ids', break each id set into batches to satisfy requirements of next API call. Relationship
    #between an Asset or Fund that may have been updated and the resulting updates is one to many.
    updated_data_ids_arr = updated_data_ids.int
    LOGGER.info('API call: A total of %s standardized data items were returned.', len(updated_data_ids))
    id_set_chunks = __split_id_set(updated_data_ids_arr, MAX_ID_CHUNK_SIZE)
    LOGGER.info('Preparing to process results of standardized id fetches, total number of sets %s', len(id_set_chunks))

    for id_set in id_set_chunks:
        with metrics.record_counter('periodic data fetch') as counter:
            update_count = update_count + __process_iget_batch_set(id_set, req_state)

    return update_count

def __process_iget_batch_set(ids, req_state):
    """Given a set of stanardized data id's, retrieve associated data for the ids, and perform a publish to singer.
    Call is limited to 20k batches, so method requires criteria to be less than the max permitable limit. """
    LOGGER.info('Processing iget batch request for %s record set', len(ids))
    update_count = 0
    dataValueTypes = req_state.client.factory.create('DataValueTypes')
    iGetParamsList = req_state.client.factory.create('ArrayOfBaseRequestParameters')
    req_id = 0

    if ids==None or len(ids)==0:
        return 0

    # For each standardized data id, format into soap request envelope and add to collection.
    for id in ids:
        req_id = req_id + 1
        iGetParams = req_state.client.factory.create('AssetAndFundGetRequestParameters')
        iGetParams.StandardizedDataId = id
        iGetParams.RequestIdentifier = req_id  # Our own id?
        iGetParams.DataValueType = getattr(dataValueTypes, 'ObjectId')
        iGetParamsList.BaseRequestParameters.append(iGetParams)

    #Create request wrapper for API call
    iGetRequest = req_state.client.factory.create('DataServiceRequest')
    iGetRequest.IncludeStandardizedDataInfo = True
    iGetRequest.ParametersList = iGetParamsList

    # Perform actual SOAP request
    with metrics.http_request_timer('iget batch') as timer:
        LOGGER.info('API: Performing iget batch request for %s record set, start time: %s', len(ids), datetime.now().strftime("%m/%d/%Y, %H:%M:%S"))
        data_values = req_state.client.service.iGetBatch(iGetRequest)
        LOGGER.info('API: get batch request complete, end time: %s', datetime.now().strftime("%m/%d/%Y, %H:%M:%S"))
        if isinstance(data_values, str):
            return 0
        result_records = data_values[0]
        LOGGER.info('Retrieved a total of %s data_values', len(result_records))

        #Publish results to Singer.
        for item in result_records:
            try:
                record = _convert_ipush_event_to_obj(item)
                transformed_record = __transform_record(record, req_state.stream, 'none')
                __write_record(req_state.stream_name, transformed_record, utils.now())
                update_count = update_count + 1

            except Exception as err:
                err_msg = 'error during transformation for entity (periodic data): ' \
                    .format(err, transformed_record)
                LOGGER.error(err_msg)
        LOGGER.info('Request time %s', timer.elapsed)
    return update_count

def _convert_ipush_event_to_obj(event):
    """Given an object returned from the SOAP API, convert into simplified object intended for publishing to Singer."""
    result = IGetFormula()
    if isinstance(event.Value, datetime):
        result.Value = __convert_iso_8601_date(event.Value)
    elif isinstance(event.Value, int) or isinstance(event.Value, float):
        result.Value = str(event.Value)
    else:
        result.Value = event.Value

    result.DataItemId = event.SDParameters.DataItemId
    result.PeriodEnd = __convert_iso_8601_date(event.SDParameters.EndOfPeriod.Value)
    result.ReportedDate = __convert_iso_8601_date(event.SDParameters.ReportedDate.Value)
    result.ScenarioId = event.SDParameters.ScenarioId
    result.EntitiesPath = event.SDParameters.EntitiesPath.Path.int
    result.DataValueType = event.SDParameters.DataValueType
    result.StandardizedDataId = event.SDParameters.StandardizedDataId
    return result