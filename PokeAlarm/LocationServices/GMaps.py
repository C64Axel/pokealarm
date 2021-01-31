# inspired by https://github.com/TiMXL73
# use this only for private Nominatim Server
# put your Serverstring,Username,Password in your gmaps-key and set gmaps-rev-geocode = yes
# exampe with authenticatio: gmaps-key: <USER>:<PASSWORD>@https://<SERVER>
# exampe without authenticatio: gmaps-key: https://<SERVER>

# Standard Library Imports
import collections
from datetime import datetime, timedelta
import logging
import time
import json
import traceback
# 3rd Party Imports
import requests
from requests.packages.urllib3.util.retry import Retry
from gevent.lock import Semaphore
# Local Imports
from PokeAlarm import Unknown
from PokeAlarm.Utilities.GenUtils import synchronize_with

log = logging.getLogger('Gmaps')


class GMaps(object):

    # Maximum number of requests per second
    _queries_per_second = 50
    # How often to warn about going over query limit
    _warning_window = timedelta(minutes=1)

    def __init__(self, api_key):
        self._key = api_key
        self._lock = Semaphore

        # Create a session to handle connections
        self._session = self._create_session()

        # Sliding window for rate limiting
        self._window = collections.deque(maxlen=self._queries_per_second)
        self._time_limit = datetime.utcnow()

        # Memoization dicts
        self._geocode_hist = {}
        self._reverse_geocode_hist = {}

    # TODO: Move into utilities
    @staticmethod
    def _create_session(retry_count=3, pool_size=3, backoff=.25):
        """ Create a session to use connection pooling. """

        # Create a session for connection pooling and
        session = requests.Session()

        # Reattempt connection on these statuses
        status_forcelist = [500, 502, 503, 504]

        # Define a Retry object to handle failures
        retry_policy = Retry(
            total=retry_count,
            backoff_factor=backoff,
            status_forcelist=status_forcelist
        )

        # Define an Adapter, to limit pool and implement retry policy
        adapter = requests.adapters.HTTPAdapter(
            max_retries=retry_policy,
            pool_connections=pool_size,
            pool_maxsize=pool_size
        )

        # Apply Adapter for all HTTPS (no HTTP for you!)
        session.mount('https://', adapter)

        return session

    def _make_request(self, service, params=None):
        """ Make a request to the GMAPs API. """
        # Rate Limit - All APIs use the same quota
        if len(self._window) == self._queries_per_second:
            # Calculate elapsed time since start of window
            elapsed_time = time.time() - self._window[0]
            if elapsed_time < 1:
                # Sleep off the difference
                time.sleep(1 - elapsed_time)

        # Create the correct url
        if '@' in self._key:
            userpassword = self._key.split("@")[0]
            url = self._key.split("@")[1] + "/" + service
        else:
            userpassword = False
            url = self._key.split("@")[0] + "/" + service
        
        # Use the session to send the request
        log.debug('{} request sending.'.format(service))
        self._window.append(time.time())
        if userpassword:
            request = self._session.get(url, params=params, auth=(userpassword.split(":")[0], userpassword.split(":")[1]), timeout=3, verify=False)
        else:
            request = self._session.get(url, params=params, timeout=3, verify=False)
        if not request.ok:
            log.debug('Response body: {}'.format(
                json.dumps(request.json(), indent=4, sort_keys=True)))
            # Raise HTTPError
            request.raise_for_status()

        log.debug('{} request completed successfully with response {}.'
                  ''.format(service, request.status_code))
        body = request.json()

        if type(body) is list:
            body = body[0]
            
        if 'error' not in body:
            return body
        else:
            if body['error'] == 'Unable to geocode':
                return {}
            else:
                raise ValueError('Unexpected response status:\n {}'.format(body))

    @synchronize_with()
    def geocode(self, address, language='en'):
        # type: (str, str) -> tuple
        """ Returns 'lat,lng' associated with the name of the place. """
        # Check for memoized results
        address = address.lower()
        if address in self._geocode_hist:
            return self._geocode_hist[address]
        # Set default in case something happens
        latlng = None
        try:
            # Set parameters and make the request
            params = {'q': address, 'accept-language': language, format: 'json'}
            response = self._make_request('search', params)
            # Extract the results and format into a dict
            if 'lat' in response and 'lng' in response:
                latlng = float(response['lat']), float(response['lng'])

            # Memoize the results
            self._geocode_hist[address] = latlng
        except requests.exceptions.HTTPError as e:
            log.error("Geocode failed with "
                      "HTTPError: {}".format(e.message))
        except requests.exceptions.Timeout as e:
            log.error("Geocode failed with "
                      "connection issues: {}".format(e.message))
        except UserWarning:
            log.error("Geocode failed because of exceeded quota.")
        except Exception as e:
            log.error("Geocode failed because "
                      "unexpected error has occurred: "
                      "{} - {}".format(type(e).__name__, e.message))
            log.error("Stack trace: \n {}".format(traceback.format_exc()))
        # Send back tuple
        return latlng

    _reverse_geocode_defaults = {
        'street_num': Unknown.SMALL,
        'street': Unknown.REGULAR,
        'address': Unknown.REGULAR,
        'address_eu': Unknown.REGULAR,
        'postal': Unknown.REGULAR,
        'neighborhood': Unknown.REGULAR,
        'sublocality': Unknown.REGULAR,
        'city': Unknown.REGULAR,
        'county': Unknown.REGULAR,
        'state': Unknown.REGULAR,
        'country': Unknown.REGULAR
    }

    @synchronize_with()
    def reverse_geocode(self, latlng, language='en'):
        # type: (tuple) -> dict
        """ Returns the reverse geocode DTS associated with 'lat,lng'. """
        latlng_hist = '{:.5f},{:.5f}'.format(latlng[0], latlng[1])
        # Check for memoized results
        if latlng_hist in self._reverse_geocode_hist:
            return self._reverse_geocode_hist[latlng_hist]
        # Get defaults in case something happens
        dts = self._reverse_geocode_defaults.copy()
        try:
            # Set parameters and make the request
            params = {'lat': latlng[0], 'lon': latlng[1], 'accept-language': language, 'format': 'json'}
            response = self._make_request('reverse', params)
            # Extract the results and format into a dict
            if 'address' in response:
                details = response.get('address', [])
                # Note: for addresses on unnamed roads, EMPTY is preferred for
                # 'street_num' and 'street' to avoid DTS looking weird
                print(details)
                dts['street_num'] = details.get('house_number', details.get('house_name', Unknown.EMPTY))
                dts['street'] = details.get('road', details.get('street', details.get('city_block', details.get('retail', Unknown.EMPTY))))
                dts['address'] = u"{} {}".format(dts['street_num'], dts['street'])
                dts['address_eu'] = u"{} {}".format(dts['street'], dts['street_num'])  # Europeans are backwards
                dts['postal'] = details.get('postcode', Unknown.REGULAR)
                dts['country'] = details.get('country', details.get('country_code', Unknown.REGULAR))
                dts['state'] = details.get('region', details.get('state', Unknown.REGULAR))
                dts['city'] = details.get('village', details.get('town', details.get('city', details.get('municipality', Unknown.REGULAR))))
                dts['county'] = details.get('county', details.get('state_district', Unknown.REGULAR))
                dts['neighborhood'] = details.get('neighbourhood', details.get('allotments', details.get('quarter', Unknown.REGULAR)))
                dts['sublocality'] = details.get('city_district', details.get('district', details.get('borough', details.get('suburb', details.get('subdivision', Unknown.REGULAR)))))
            # Memoize the results
            self._reverse_geocode_hist[latlng] = dts
        except requests.exceptions.HTTPError as e:
            log.error("Reverse Geocode failed with "
                      "HTTPError: {}".format(e.message))
        except requests.exceptions.Timeout as e:
            log.error("Reverse Geocode failed with "
                      "connection issues: {}".format(e.message))
        except UserWarning:
            log.error("Reverse Geocode failed because of exceeded quota.")
        except Exception as e:
            log.error("Reverse Geocode failed because "
                      "unexpected error has occurred: "
                      "{} - {}".format(type(e).__name__, e.message))
            log.error("Stack trace: \n {}".format(traceback.format_exc()))
        # Send back dts
        return dts

    @synchronize_with()
    def distance_matrix(self, mode, origin, dest, lang, units):

        dts = {'': Unknown.REGULAR, '': Unknown.REGULAR}
        
        # Removed API Calls Since Unsupported by Nominatim
        log.error("Distance calls unsupported. Returning default of unknown")

        # Send back DTS
        return dts