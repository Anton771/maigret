#! /usr/bin/env python3

"""
Maigret (Sherlock fork): Find Usernames Across Social Networks Module

This module contains the main logic to search for usernames at social
networks.
"""

import csv
import json
import logging
import os
import platform
import re
import sys
from argparse import ArgumentParser, RawDescriptionHelpFormatter
from time import monotonic
from http.cookies import SimpleCookie

import requests
from socid_extractor import parse, extract

from requests_futures.sessions import FuturesSession
from torrequest import TorRequest
from result import QueryStatus
from result import QueryResult
from notify import QueryNotifyPrint
from sites  import SitesInformation

module_name = "Maigret (Sherlock fork): Find Usernames Across Social Networks"
__version__ = "0.12.2"



class SherlockFuturesSession(FuturesSession):
    def request(self, method, url, hooks={}, *args, **kwargs):
        """Request URL.

        This extends the FuturesSession request method to calculate a response
        time metric to each request.

        It is taken (almost) directly from the following StackOverflow answer:
        https://github.com/ross/requests-futures#working-in-the-background

        Keyword Arguments:
        self                   -- This object.
        method                 -- String containing method desired for request.
        url                    -- String containing URL for request.
        hooks                  -- Dictionary containing hooks to execute after
                                  request finishes.
        args                   -- Arguments.
        kwargs                 -- Keyword arguments.

        Return Value:
        Request object.
        """
        #Record the start time for the request.
        start = monotonic()

        def response_time(resp, *args, **kwargs):
            """Response Time Hook.

            Keyword Arguments:
            resp                   -- Response object.
            args                   -- Arguments.
            kwargs                 -- Keyword arguments.

            Return Value:
            N/A
            """
            resp.elapsed = monotonic() - start

            return

        #Install hook to execute when response completes.
        #Make sure that the time measurement hook is first, so we will not
        #track any later hook's execution time.
        try:
            if isinstance(hooks['response'], list):
                hooks['response'].insert(0, response_time)
            elif isinstance(hooks['response'], tuple):
                #Convert tuple to list and insert time measurement hook first.
                hooks['response'] = list(hooks['response'])
                hooks['response'].insert(0, response_time)
            else:
                #Must have previously contained a single hook function,
                #so convert to list.
                hooks['response'] = [response_time, hooks['response']]
        except KeyError:
            #No response hook was already defined, so install it ourselves.
            hooks['response'] = [response_time]

        return super(SherlockFuturesSession, self).request(method,
                                                           url,
                                                           hooks=hooks,
                                                           *args, **kwargs)


def get_response(request_future, error_type, social_network):

    #Default for Response object if some failure occurs.
    response = None

    error_context = "General Unknown Error"
    expection_text = None
    try:
        response = request_future.result()
        if response.status_code:
            #status code exists in response object
            error_context = None
    except requests.exceptions.HTTPError as errh:
        error_context = "HTTP Error"
        expection_text = str(errh)
    except requests.exceptions.ProxyError as errp:
        error_context = "Proxy Error"
        expection_text = str(errp)
    except requests.exceptions.ConnectionError as errc:
        error_context = "Error Connecting"
        expection_text = str(errc)
    except requests.exceptions.Timeout as errt:
        error_context = "Timeout Error"
        expection_text = str(errt)
    except requests.exceptions.RequestException as err:
        error_context = "Unknown Error"
        expection_text = str(err)

    return response, error_context, expection_text


def sherlock(username, site_data, query_notify,
             tor=False, unique_tor=False,
             proxy=None, timeout=None, ids_search=False,
             id_type='username',tags=[]):
    """Run Sherlock Analysis.

    Checks for existence of username on various social media sites.

    Keyword Arguments:
    username               -- String indicating username that report
                              should be created against.
    site_data              -- Dictionary containing all of the site data.
    query_notify           -- Object with base type of QueryNotify().
                              This will be used to notify the caller about
                              query results.
    tor                    -- Boolean indicating whether to use a tor circuit for the requests.
    unique_tor             -- Boolean indicating whether to use a new tor circuit for each request.
    proxy                  -- String indicating the proxy URL
    timeout                -- Time in seconds to wait before timing out request.
                              Default is no timeout.
    ids_search             -- Search for other usernames in website pages & recursive search by them.

    Return Value:
    Dictionary containing results from report. Key of dictionary is the name
    of the social network site, and the value is another dictionary with
    the following keys:
        url_main:      URL of main site.
        url_user:      URL of user on site (if account exists).
        status:        QueryResult() object indicating results of test for
                       account existence.
        http_status:   HTTP status code of query which checked for existence on
                       site.
        response_text: Text that came back from request.  May be None if
                       there was an HTTP error when checking for existence.
    """

    #Notify caller that we are starting the query.
    query_notify.start(username, id_type)

    # Create session based on request methodology
    if tor or unique_tor:
        #Requests using Tor obfuscation
        underlying_request = TorRequest()
        underlying_session = underlying_request.session
    else:
        #Normal requests
        underlying_session = requests.session()
        underlying_request = requests.Request()

    #Limit number of workers to 20.
    #This is probably vastly overkill.
    if len(site_data) >= 20:
        max_workers=20
    else:
        max_workers=len(site_data)

    #Create multi-threaded session for all requests.
    session = SherlockFuturesSession(max_workers=max_workers,
                                     session=underlying_session)


    # Results from analysis of all sites
    results_total = {}

    # First create futures for all requests. This allows for the requests to run in parallel
    for social_network, net_info in site_data.items():

        # print(id_type) # print(social_network)
        if net_info.get('type', 'username') != id_type:
            continue

        site_tags = set(net_info.get('tags', []))
        if tags:
            if not tags.intersection(site_tags):
                continue

        # Results from analysis of this specific site
        results_site = {}

        # Record URL of main site
        results_site['url_main'] = net_info.get("urlMain")

        # A user agent is needed because some sites don't return the correct
        # information since they think that we are bots (Which we actually are...)
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 11.1; rv:55.0) Gecko/20100101 Firefox/55.0',
        }

        if "headers" in net_info:
            # Override/append any extra headers required by a given site.
            headers.update(net_info["headers"])

        # URL of user on site (if it exists)
        url = net_info.get('url').format(username)

        # Don't make request if username is invalid for the site
        regex_check = net_info.get("regexCheck")
        if regex_check and re.search(regex_check, username) is None:
            # No need to do the check at the site: this user name is not allowed.
            results_site['status'] = QueryResult(username,
                                                 social_network,
                                                 url,
                                                 QueryStatus.ILLEGAL)
            results_site["url_user"] = ""
            results_site['http_status'] = ""
            results_site['response_text'] = ""
            query_notify.update(results_site['status'])
        else:
            # URL of user on site (if it exists)
            results_site["url_user"] = url
            url_probe = net_info.get("urlProbe")
            if url_probe is None:
                # Probe URL is normal one seen by people out on the web.
                url_probe = url
            else:
                # There is a special URL for probing existence separate
                # from where the user profile normally can be found.
                url_probe = url_probe.format(username)

            if (net_info["errorType"] == 'status_code' and 
                net_info.get("request_head_only", True) == True):
                #In most cases when we are detecting by status code,
                #it is not necessary to get the entire body:  we can
                #detect fine with just the HEAD response.
                request_method = session.head
            else:
                #Either this detect method needs the content associated
                #with the GET response, or this specific website will
                #not respond properly unless we request the whole page.
                request_method = session.get

            if net_info["errorType"] == "response_url":
                # Site forwards request to a different URL if username not
                # found.  Disallow the redirect so we can capture the
                # http status from the original URL request.
                allow_redirects = False
            else:
                # Allow whatever redirect that the site wants to do.
                # The final result of the request will be what is available.
                allow_redirects = True

            def parse_cookies(cookies_str):
                cookies = SimpleCookie()
                cookies.load(cookies_str)
                return {key: morsel.value for key, morsel in cookies.items()}

            # cookies_str = 'collections_gid=117041; cph=948; cpw=550; yandexuid=6894705951593339704; i=NJAxWCDEQdhKbNGBppYN/5sl4XuX2Lq/lgZELKOVfjfX3boBnqOMyP0s0MSwcBbeuqPaRqjWPrsSXORVLDlLJ7Qi+RI=; font_loaded=YSv1; yuidss=6894705951593339704; ymex=1908699704.yrts.1593339704; _ym_wasSynced=%7B%22time%22%3A1593339704889%2C%22params%22%3A%7B%22eu%22%3A0%7D%2C%22bkParams%22%3A%7B%7D%7D; gdpr=0; _ym_uid=1593339705197323602; _ym_d=1593339705; mda=0; _ym_isad=2; ar=1593339710541646-252043; _ym_visorc_10630330=b; spravka=dD0xNTkzMzM5NzIyO2k9NS4yMjguMjI0LjM3O3U9MTU5MzMzOTcyMjM2MDI4NTg0MjtoPWM3NThkYjU0MzYyMzViZDEwMzU3ZGY3NTUwYzViNDE1'
            cookies_str = ''

            if 'yandex' in url_probe:
                # import logging
                # logging.error(cookies_str)
                cookies = parse_cookies(cookies_str)
            else:
                cookies = None

            # This future starts running the request in a new thread, doesn't block the main thread
            if proxy is not None:
                proxies = {"http": proxy, "https": proxy}
                future = request_method(url=url_probe, headers=headers,
                                        proxies=proxies,
                                        allow_redirects=allow_redirects,
                                        timeout=timeout,
                                        # cookies=cookies
                                        )
            else:
                future = request_method(url=url_probe, headers=headers,
                                        allow_redirects=allow_redirects,
                                        timeout=timeout,
                                        # cookies=cookies
                                        )

            # Store future in data for access later
            net_info["request_future"] = future

            # Reset identify for tor (if needed)
            if unique_tor:
                underlying_request.reset_identity()

        # Add this site's results into final dictionary with all of the other results.
        results_total[social_network] = results_site

    # Open the file containing account links
    # Core logic: If tor requests, make them here. If multi-threaded requests, wait for responses
    for social_network, net_info in site_data.items():

        # Retrieve results again
        results_site = results_total.get(social_network)
        if not results_site:
            continue

        # Retrieve other site information again
        url = results_site.get("url_user")
        status = results_site.get("status")
        if status is not None:
            # We have already determined the user doesn't exist here
            continue

        # Get the expected error type
        error_type = net_info["errorType"]

        # Get the failure messages and comments
        failure_errors = net_info.get("errors", {})

        # Retrieve future and ensure it has finished
        future = net_info["request_future"]
        r, error_text, expection_text = get_response(request_future=future,
                                                     error_type=error_type,
                                                     social_network=social_network)

        #Get response time for response of our request.
        try:
            response_time = r.elapsed
        except AttributeError:
            response_time = None

        # Attempt to get request information
        try:
            http_status = r.status_code
        except:
            http_status = "?"
        try:
            response_text = r.text.encode(r.encoding)
            # Extract IDs data from page
        except:
            response_text = ""


        # Detect failures such as a country restriction
        for text, comment in failure_errors.items():
            if text in r.text:
                error_context = "Some error"
                error_text = comment
                break

        # TODO: return error for captcha and some specific cases (CashMe)
        # make all result invalid

        extracted_ids_data = ""

        if ids_search and r:
            # print(r.text)
            extracted_ids_data = extract(r.text)

            if extracted_ids_data:
                new_usernames = {}
                for k,v in extracted_ids_data.items():
                    if 'username' in k:
                        new_usernames[v] = 'username'
                    if k in ('yandex_public_id', 'wikimapia_uid', 'gaia_id'):
                        new_usernames[v] = k

                results_site['ids_usernames'] = new_usernames

        if error_text is not None:
            result = QueryResult(username,
                                 social_network,
                                 url,
                                 QueryStatus.UNKNOWN,
                                 ids_data=extracted_ids_data,
                                 query_time=response_time,
                                 context=error_text)
        elif error_type == "message":
            error = net_info.get("errorMsg")
            errors_set = set(error) if type(error) == list else set({error})
            # Checks if the error message is in the HTML
            error_found = any([(err in r.text) for err in errors_set])
            if not error_found:
                result = QueryResult(username,
                                     social_network,
                                     url,
                                     QueryStatus.CLAIMED,
                                     ids_data=extracted_ids_data,
                                     query_time=response_time)
            else:
                result = QueryResult(username,
                                     social_network,
                                     url,
                                     QueryStatus.AVAILABLE,
                                     ids_data=extracted_ids_data,
                                     query_time=response_time)
        elif error_type == "status_code":
            # Checks if the status code of the response is 2XX
            if not r.status_code >= 300 or r.status_code < 200:
                result = QueryResult(username,
                                     social_network,
                                     url,
                                     QueryStatus.CLAIMED,
                                     ids_data=extracted_ids_data,
                                     query_time=response_time)
            else:
                result = QueryResult(username,
                                     social_network,
                                     url,
                                     QueryStatus.AVAILABLE,
                                     ids_data=extracted_ids_data,
                                     query_time=response_time)
        elif error_type == "response_url":
            # For this detection method, we have turned off the redirect.
            # So, there is no need to check the response URL: it will always
            # match the request.  Instead, we will ensure that the response
            # code indicates that the request was successful (i.e. no 404, or
            # forward to some odd redirect).
            if 200 <= r.status_code < 300:
                result = QueryResult(username,
                                     social_network,
                                     url,
                                     QueryStatus.CLAIMED,
                                     ids_data=extracted_ids_data,
                                     query_time=response_time)
            else:
                result = QueryResult(username,
                                     social_network,
                                     url,
                                     QueryStatus.AVAILABLE,
                                     ids_data=extracted_ids_data,
                                     query_time=response_time)
        else:
            #It should be impossible to ever get here...
            raise ValueError(f"Unknown Error Type '{error_type}' for "
                             f"site '{social_network}'")


        #Notify caller about results of query.
        query_notify.update(result)

        # Save status of request
        results_site['status'] = result

        # Save results from request
        results_site['http_status'] = http_status
        results_site['response_text'] = response_text

        # Add this site's results into final dictionary with all of the other results.
        results_total[social_network] = results_site

    #Notify caller that all queries are finished.
    query_notify.finish()

    return results_total


def timeout_check(value):
    """Check Timeout Argument.

    Checks timeout for validity.

    Keyword Arguments:
    value                  -- Time in seconds to wait before timing out request.

    Return Value:
    Floating point number representing the time (in seconds) that should be
    used for the timeout.

    NOTE:  Will raise an exception if the timeout in invalid.
    """
    from argparse import ArgumentTypeError

    try:
        timeout = float(value)
    except:
        raise ArgumentTypeError(f"Timeout '{value}' must be a number.")
    if timeout <= 0:
        raise ArgumentTypeError(f"Timeout '{value}' must be greater than 0.0s.")
    return timeout


def main():

    version_string = f"%(prog)s {__version__}\n" +  \
                     f"{requests.__description__}:  {requests.__version__}\n" + \
                     f"Python:  {platform.python_version()}"

    parser = ArgumentParser(formatter_class=RawDescriptionHelpFormatter,
                            description=f"{module_name} (Version {__version__})"
                            )
    parser.add_argument("--version",
                        action="version",  version=version_string,
                        help="Display version information and dependencies."
                        )
    parser.add_argument("--verbose", "-v", "-d", "--debug",
                        action="store_true",  dest="verbose", default=False,
                        help="Display extra debugging information and metrics."
                        )
    parser.add_argument("--rank", "-r",
                        action="store_true", dest="rank", default=False,
                        help="Present websites ordered by their Alexa.com global rank in popularity.")
    parser.add_argument("--folderoutput", "-fo", dest="folderoutput",
                        help="If using multiple usernames, the output of the results will be saved to this folder."
                        )
    parser.add_argument("--output", "-o", dest="output",
                        help="If using single username, the output of the result will be saved to this file."
                        )
    parser.add_argument("--tor", "-t",
                        action="store_true", dest="tor", default=False,
                        help="Make requests over Tor; increases runtime; requires Tor to be installed and in system path.")
    parser.add_argument("--unique-tor", "-u",
                        action="store_true", dest="unique_tor", default=False,
                        help="Make requests over Tor with new Tor circuit after each request; increases runtime; requires Tor to be installed and in system path.")
    parser.add_argument("--csv",
                        action="store_true",  dest="csv", default=False,
                        help="Create Comma-Separated Values (CSV) File."
                        )
    parser.add_argument("--site",
                        action="append", metavar='SITE_NAME',
                        dest="site_list", default=None,
                        help="Limit analysis to just the listed sites. Add multiple options to specify more than one site."
                        )
    parser.add_argument("--proxy", "-p", metavar='PROXY_URL',
                        action="store", dest="proxy", default=None,
                        help="Make requests over a proxy. e.g. socks5://127.0.0.1:1080"
                        )
    parser.add_argument("--json", "-j", metavar="JSON_FILE",
                        dest="json_file", default=None,
                        help="Load data from a JSON file or an online, valid, JSON file.")
    parser.add_argument("--timeout",
                        action="store", metavar='TIMEOUT',
                        dest="timeout", type=timeout_check, default=None,
                        help="Time (in seconds) to wait for response to requests. "
                             "Default timeout of 60.0s."
                             "A longer timeout will be more likely to get results from slow sites."
                             "On the other hand, this may cause a long delay to gather all results."
                        )
    parser.add_argument("--print-found",
                        action="store_true", dest="print_found_only", default=False,
                        help="Do not output sites where the username was not found."
                        )
    parser.add_argument("--skip-errors",
                        action="store_true", dest="skip_check_errors", default=False,
                        help="Do not print errors messages: connection, captcha, site country ban, etc."
                        )
    parser.add_argument("--no-color",
                        action="store_true", dest="no_color", default=False,
                        help="Don't color terminal output"
                        )
    parser.add_argument("--browse", "-b",
                        action="store_true", dest="browse", default=False,
                        help="Browse to all results on default bowser."
                        )
    parser.add_argument("--ids", "-i",
                        action="store_true", dest="ids_search", default=False,
                        help="Make scan of pages for other usernames and recursive search by them."
                        )
    parser.add_argument("--parse",
                        dest="parse_url", default='',
                        help="Parse page by URL and extract username and IDs to use for search."
                        )
    parser.add_argument("username",
                        nargs='+', metavar='USERNAMES',
                        action="store",
                        help="One or more usernames to check with social networks."
                        )
    parser.add_argument("--tags",
                        dest="tags", default='',
                        help="Specify tags of sites."
                        )

    args = parser.parse_args()
    # Argument check

    # Usernames initial list
    usernames = {
        u: 'username'
        for u in args.username
        if u not in ('-')
    }

    # TODO regex check on args.proxy
    if args.tor and (args.proxy is not None):
        raise Exception("Tor and Proxy cannot be set at the same time.")

    # Make prompts
    if args.proxy is not None:
        print("Using the proxy: " + args.proxy)

    if args.tor or args.unique_tor:
        print("Using Tor to make requests")
        print("Warning: some websites might refuse connecting over Tor, so note that using this option might increase connection errors.")

    # Check if both output methods are entered as input.
    if args.output is not None and args.folderoutput is not None:
        print("You can only use one of the output methods.")
        sys.exit(1)

    # Check validity for single username output.
    if args.output is not None and len(args.username) != 1:
        print("You can only use --output with a single username")
        sys.exit(1)

    if args.parse_url:
        page, _ = parse(args.parse_url, cookies_str='collections_gid=213; cph=948; cpw=790; yandexuid=2146767031582893378; yuidss=2146767031582893378; gdpr=0; _ym_uid=1582893380492618461; mda=0; ymex=1898253380.yrts.1582893380#1900850969.yrtsi.1585490969; font_loaded=YSv1; yandex_gid=213; my=YwA=; _ym_uid=1582893380492618461; _ym_d=1593451737; L=XGJfaARJWEAARGILWAQKbXJUUU5NSEJHNAwrIxkaE11SHD4P.1593608730.14282.352228.74f1540484d115d5f534c370a0d54d14; yandex_login=danilovdelta; i=pQT2fDoFQAd1ZkIJW/qOXaKw+KI7LXUGoTQbUy5dPTdftfK7HFAnktwsf4MrRy4aQEk0sqxbZGY18+bnpKkrDgt29/8=; ys=udn.cDpkYW5pbG92ZGVsdGE%3D#wprid.1593608013100941-1715475084842016754100299-production-app-host-man-web-yp-306#ymrefl.DD2F275B69BCF594; zm=m-white_bender.webp.css-https%3As3home-static_KgOlxZDBNvw0efFr5riblj4yPtY%3Al; yp=1908968730.udn.cDpkYW5pbG92ZGVsdGE%3D#1595886694.ygu.1#1609637986.szm.2:1680x1050:1644x948#1596131262.csc.2#1908664615.sad.1593304615:1593304615:1#1908965951.multib.1; _ym_d=1593869990; yc=1594225567.zen.cach%3A1593969966; yabs-frequency=/5/0m0004s7_5u00000/8Y10RG00003uEo7ptt9m00000FWx8KRMFsq00000w3j-/; ys_fp=form-client%3DWeb%26form-page%3Dhttps%253A%252F%252Fyandex.ru%252Fchat%2523%252F%2540%252Fchats%252F1%25252F0%25252F964d3b91-5972-49c2-84d3-ed614622223f%2520%25D0%25AF%25D0%25BD%25D0%25B4%25D0%25B5%25D0%25BA%25D1%2581.%25D0%259C%25D0%25B5%25D1%2581%25D1%2581%25D0%25B5%25D0%25BD%25D0%25B4%25D0%25B6%25D0%25B5%25D1%2580%26form-referrer%3Dhttps%253A%252F%252Fyandex.ru%252Fchat%26form-browser%3DMozilla%252F5.0%2520(Macintosh%253B%2520Intel%2520Mac%2520OS%2520X%252010_15_5)%2520AppleWebKit%252F537.36%2520(KHTML%252C%2520like%2520Gecko)%2520Chrome%252F83.0.4103.116%2520Safari%252F537.36%26form-screen%3D1680%25C3%25971050%25C3%259730%26form-window%3D792%25C3%2597948%26form-app_version%3D2.8.0%26form-reqid%3D1593966167731077-1230441077775610555700303-production-app-host-sas-web-yp-249; skid=8069161091593972389; device_id="a9eb41b4cb3b056e5da4f9a4029a9e7cfea081196"; cycada=xPXy0sesbr5pVmRDiBiYZnAFhHtmn6zZ/YSDpCUU2Gs=; Session_id=3:1594143924.5.1.1593295629841:JeDkBQ:f.1|611645851.-1.0.1:114943352|33600788.310322.2.2:310322|219601.339772.5aiiRX9iIGUU6gzDuKnO4dqTM24; sessionid2=3:1594143924.5.1.1593295629841:JeDkBQ:f.1|611645851.-1.0.1:114943352|33600788.310322.2.2:310322|219601.678091.QGFa-AEA5z46AzNAmKFAL4_4jdM; _ym_isad=2; active-browser-timestamp=1594143926414; q-csrf-token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiIzMzYwMDc4OCIsImV4cCI6MTU5NDIzMDMzMX0.w4FkWaag4t1D7j42MD2ILP0oenqZiIjo4iOZnshCiwY; ar=1594145799547993-792214; _ym_visorc_10630330=w; spravka=dD0xNTk0MTQ1ODMwO2k9NS4yMjguMjI0LjM3O3U9MTU5NDE0NTgzMDI5MTk5NTkwMjtoPWMyZTI1Mjk4NmVmZjFhNGNjMGZhYmIwZWQ3ZDIyMmZk')
        info = extract(page)
        text = 'Extracted ID data from webpage: ' + ', '.join([f'{a}: {b}' for a,b in info.items()])
        print(text)
        for k, v in info.items():
            if 'username' in k:
                usernames[v] = 'username'
            if k in ('yandex_public_id', 'wikimapia_uid', 'gaia_id'):
                usernames[v] = k

    if args.tags:
        args.tags = set(args.tags.split(','))

    #Create object with all information about sites we are aware of.
    try:
        sites = SitesInformation(args.json_file)
    except Exception as error:
        print(f"ERROR:  {error}")
        sys.exit(1)

    #Create original dictionary from SitesInformation() object.
    #Eventually, the rest of the code will be updated to use the new object
    #directly, but this will glue the two pieces together.
    site_data_all = {}
    for site in sites:
        site_data_all[site.name] = site.information

    if args.site_list is None:
        # Not desired to look at a sub-set of sites
        site_data = site_data_all
    else:
        # User desires to selectively run queries on a sub-set of the site list.

        # Make sure that the sites are supported & build up pruned site database.
        site_data = {}
        site_missing = []
        for site in args.site_list:
            for existing_site in site_data_all:
                if site.lower() == existing_site.lower():
                    site_data[existing_site] = site_data_all[existing_site]
            if not site_data:
                # Build up list of sites not supported for future error message.
                site_missing.append(f"'{site}'")

        if site_missing:
            print(
                f"Error: Desired sites not found: {', '.join(site_missing)}.")
            sys.exit(1)

    if args.rank:
        # Sort data by rank
        site_dataCpy = dict(site_data)
        ranked_sites = sorted(site_data, key=lambda k: ("rank" not in k, site_data[k].get("rank", sys.maxsize)))
        site_data = {}
        for site in ranked_sites:
            site_data[site] = site_dataCpy.get(site)


    #Create notify object for query results.
    query_notify = QueryNotifyPrint(result=None,
                                    verbose=args.verbose,
                                    print_found_only=args.print_found_only,
                                    skip_check_errors=args.skip_check_errors,
                                    color=not args.no_color)

    already_checked = set()

    while usernames:
        username, id_type = list(usernames.items())[0]
        del usernames[username]

        if username.lower() in already_checked:
            continue
        else:
            already_checked.add(username.lower())

        results = sherlock(username,
                           site_data,
                           query_notify,
                           tor=args.tor,
                           unique_tor=args.unique_tor,
                           proxy=args.proxy,
                           timeout=args.timeout,
                           ids_search=args.ids_search,
                           id_type=id_type,
                           tags=args.tags)


        if args.output:
            result_file = args.output
        elif args.folderoutput:
            # The usernames results should be stored in a targeted folder.
            # If the folder doesn't exist, create it first
            os.makedirs(args.folderoutput, exist_ok=True)
            result_file = os.path.join(args.folderoutput, f"{username}.txt")
        else:
            result_file = f"{username}.txt"

        with open(result_file, "w", encoding="utf-8") as file:
            exists_counter = 0
            for website_name in results:
                dictionary = results[website_name]

                new_usernames = dictionary.get('ids_usernames')
                if new_usernames:
                    for u, utype in new_usernames.items():
                        usernames[u] = utype

                if dictionary.get("status").status == QueryStatus.CLAIMED:
                    exists_counter += 1
                    file.write(dictionary["url_user"] + "\n")
            file.write(f"Total Websites Username Detected On : {exists_counter}")

        if args.csv:
            with open(username + ".csv", "w", newline='', encoding="utf-8") as csv_report:
                writer = csv.writer(csv_report)
                writer.writerow(['username',
                                 'name',
                                 'url_main',
                                 'url_user',
                                 'exists',
                                 'http_status',
                                 'response_time_s'
                                 ]
                                )
                for site in results:
                    response_time_s = results[site]['status'].query_time
                    if response_time_s is None:
                        response_time_s = ""
                    writer.writerow([username,
                                     site,
                                     results[site]['url_main'],
                                     results[site]['url_user'],
                                     str(results[site]['status'].status),
                                     results[site]['http_status'],
                                     response_time_s
                                     ]
                                    )


if __name__ == "__main__":
    main()
