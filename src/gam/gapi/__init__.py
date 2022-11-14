"""Methods related to execution of GAPI requests."""

import os.path
import sys
from tempfile import TemporaryDirectory

import googleapiclient.errors
import google.auth.exceptions
import httplib2

from gam import controlflow
from gam import display
from gam.gapi import errors
from gam import transport
from gam.var import (GC_Values, GM_Globals,
                     GM_CURRENT_API_SCOPES, GM_CURRENT_API_USER,
                     GM_EXTRA_ARGS_DICT, GM_OAUTH2SERVICE_ACCOUNT_CLIENT_ID,
                     MAX_RESULTS_API_EXCEPTIONS, MESSAGE_API_ACCESS_CONFIG,
                     MESSAGE_API_ACCESS_DENIED, MESSAGE_SERVICE_NOT_APPLICABLE)


def call(service,
         function,
         silent_errors=False,
         soft_errors=False,
         throw_reasons=None,
         retry_reasons=None,
         **kwargs):
    """Executes a single request on a Google service function.

  Args:
    service: A Google service object for the desired API.
    function: String, The name of a service request method to execute.
    silent_errors: Bool, If True, error messages are suppressed when
      encountered.
    soft_errors: Bool, If True, writes non-fatal errors to stderr.
    throw_reasons: A list of Google HTTP error reason strings indicating the
      errors generated by this request should be re-thrown. All other HTTP
      errors are consumed.
    retry_reasons: A list of Google HTTP error reason strings indicating which
      error should be retried, using exponential backoff techniques, when the
      error reason is encountered.
    **kwargs: Additional params to pass to the request method.

  Returns:
    A response object for the corresponding Google API call.
  """
    if throw_reasons is None:
        throw_reasons = []
    if retry_reasons is None:
        retry_reasons = []

    method = getattr(service, function)
    retries = 10
    parameters = dict(
        list(kwargs.items()) + list(GM_Globals[GM_EXTRA_ARGS_DICT].items()))
    for n in range(1, retries + 1):
        try:
            return method(**parameters).execute()
        except googleapiclient.errors.HttpError as e:
            http_status, reason, message = errors.get_gapi_error_detail(
                e,
                soft_errors=soft_errors,
                silent_errors=silent_errors,
                retry_on_http_error=n < 3)
            if http_status == -1:
                # The error detail indicated that we should retry this request
                # We'll refresh credentials and make another pass
                service._http.credentials.refresh(transport.create_http())
                continue
            if http_status == 0:
                return None

            is_known_error_reason = reason in [
                r.value for r in errors.ErrorReason
            ]
            if is_known_error_reason and errors.ErrorReason(
                    reason) in throw_reasons:
                if errors.ErrorReason(
                        reason) in errors.ERROR_REASON_TO_EXCEPTION:
                    raise errors.ERROR_REASON_TO_EXCEPTION[errors.ErrorReason(
                        reason)](message)
                raise e
            if (n != retries) and (is_known_error_reason and errors.ErrorReason(
                    reason) in errors.DEFAULT_RETRY_REASONS + retry_reasons):
                controlflow.wait_on_failure(n, retries, reason)
                continue
            if soft_errors:
                display.print_error(
                    f'{http_status}: {message} - {reason}{["", ": Giving up."][n > 1]}'
                )
                return None
            controlflow.system_error_exit(
                int(http_status), f'{http_status}: {message} - {reason}')
        except google.auth.exceptions.RefreshError as e:
            handle_oauth_token_error(
                e, soft_errors or
                errors.ErrorReason.SERVICE_NOT_AVAILABLE in throw_reasons)
            if errors.ErrorReason.SERVICE_NOT_AVAILABLE in throw_reasons:
                raise errors.GapiServiceNotAvailableError(str(e))
            display.print_error(
                f'User {GM_Globals[GM_CURRENT_API_USER]}: {str(e)}')
            return None
        except ValueError as e:
            if hasattr(service._http,
                       'cache') and service._http.cache is not None:
                service._http.cache = None
                continue
            controlflow.system_error_exit(4, str(e))
        except (httplib2.ServerNotFoundError, RuntimeError) as e:
            if n != retries:
                service._http.connections = {}
                controlflow.wait_on_failure(n, retries, str(e))
                continue
            controlflow.system_error_exit(4, str(e))
        except TypeError as e:
            controlflow.system_error_exit(4, str(e))


def get_items(service,
              function,
              items='items',
              throw_reasons=None,
              retry_reasons=None,
              **kwargs):
    """Gets a single page of items from a Google service function that is paged.

  Args:
    service: A Google service object for the desired API.
    function: String, The name of a service request method to execute.
    items: String, the name of the resulting "items" field within the service
      method's response object.
    throw_reasons: A list of Google HTTP error reason strings indicating the
      errors generated by this request should be re-thrown. All other HTTP
      errors are consumed.
    retry_reasons: A list of Google HTTP error reason strings indicating which
      error should be retried, using exponential backoff techniques, when the
      error reason is encountered.
    **kwargs: Additional params to pass to the request method.

  Returns:
    The list of items in the first page of a response.
  """
    results = call(service,
                   function,
                   throw_reasons=throw_reasons,
                   retry_reasons=retry_reasons,
                   **kwargs)
    if results:
        return results.get(items, [])
    return []


def _get_max_page_size_for_api_call(service, function, **kwargs):
    """Gets the maximum number of results supported for a single API call.

  Args:
    service: A Google service object for the desired API.
    function: String, The name of the service method to check for max page size.
    **kwargs: Additional params that will be passed to the request method.

  Returns:
    Int, A value from discovery if it exists, otherwise value from
        MAX_RESULTS_API_EXCEPTIONS, otherwise None
  """
    method = getattr(service, function)
    api_id = method(**kwargs).methodId
    for resource in service._rootDesc.get('resources', {}).values():
        for a_method in resource.get('methods', {}).values():
            if a_method.get('id') == api_id:
                if not a_method.get('parameters') or a_method['parameters'].get(
                        'pageSize'
                ) or not a_method['parameters'].get('maxResults'):
                    # Make sure API call supports maxResults. For now we don't care to
                    # set pageSize since all known pageSize API calls have
                    # default pageSize == max pageSize.
                    return None
                known_api_max = MAX_RESULTS_API_EXCEPTIONS.get(api_id)
                max_results = a_method['parameters']['maxResults'].get(
                    'maximum', known_api_max)
                return {'maxResults': max_results}

    return None


TOTAL_ITEMS_MARKER = '%%total_items%%'
FIRST_ITEM_MARKER = '%%first_item%%'
LAST_ITEM_MARKER = '%%last_item%%'


def got_total_items_msg(items, eol):
    """Format a page_message to be used by get_all_pages

  The page message indicates the number of items returned

  Args:
    items: String, the description of the items being returned by get_all_pages
    eol: String, the line terminator
       Values used: '', '...', '\n', '...\n'

  Returns:
    The formatted page_message
  """

    return f'Got {TOTAL_ITEMS_MARKER} {items}{eol}'


def got_total_items_first_last_msg(items):
    """Format a page_message to be used by get_all_pages

  The page message indicates the number of items returned and the
  value of the first and list items

  Args:
    items: String, the description of the items being returned by get_all_pages

  Returns:
    The formatted page_message
  """

    return f'Got {TOTAL_ITEMS_MARKER} {items}: {FIRST_ITEM_MARKER} - {LAST_ITEM_MARKER}' + '\n'


def process_page(page, items, all_items, total_items, page_message, message_attribute):
    """Process one page of a Google service function response.

  Append a list of items to the aggregate list of items

  Args:
    page: list of items
    items: see get_all_pages
    all_items: aggregate list of items
    total_items: length of all_items
    page_message: see get_all_pages
    message_attribute: get_all_pages
  Returns:
    The page token and total number of items
  """
    if page:
        page_token = page.get('nextPageToken')
        page_items = page.get(items, [])
        num_page_items = len(page_items)
        total_items += num_page_items
        if type(all_items) is list:
            all_items.extend(page_items)
        elif all_items is not None:
            i = len(all_items)
            for item in page_items:
                all_items[str(i)] = item
                i += 1
    else:
        page_token = None
        num_page_items = 0

    # Show a paging message to the user that indicates paging progress
    if page_message:
        show_message = page_message.replace(TOTAL_ITEMS_MARKER,
                                            str(total_items))
        if message_attribute:
            first_item = page_items[0] if num_page_items > 0 else {}
            last_item = page_items[-1] if num_page_items > 1 else first_item
            if isinstance(message_attribute, str):
                first_item = str(first_item.get(message_attribute, ''))
                last_item = str(last_item.get(message_attribute, ''))
            else:
                for attr in message_attribute:
                    first_item = first_item.get(attr, {})
                    last_item = last_item.get(attr, {})
                first_item = str(first_item)
                last_item = str(last_item)
            show_message = show_message.replace(FIRST_ITEM_MARKER, first_item)
            show_message = show_message.replace(LAST_ITEM_MARKER, last_item)
        sys.stderr.write('\r')
        sys.stderr.flush()
        sys.stderr.write(show_message)
    return (page_token, total_items)

def finalize_page_message(page_message):
    """ Issue final page_message """
    if page_message and (page_message[-1] != '\n'):
        sys.stderr.write('\r\n')
        sys.stderr.flush()


def get_all_pages(service,
                  function,
                  items='items',
                  page_message=None,
                  message_attribute=None,
                  soft_errors=False,
                  throw_reasons=None,
                  retry_reasons=None,
                  page_args_in_body=False,
                  **kwargs):
    """Aggregates and returns all pages of a Google service function response.

  All pages of items are aggregated and returned as a single list.

  Args:
    service: A Google service object for the desired API.
    function: String, The name of a service request method to execute.
    items: String, the name of the resulting "items" field within the method's
      response object. The items in this field will be aggregated across all
      pages and returned.
    page_message: String, a message to be displayed to the user during paging.
      Template strings allow for dynamic content to be inserted during paging.
        Supported template strings:
          TOTAL_ITEMS_MARKER : The current number of items discovered across all
            pages.
          FIRST_ITEM_MARKER  : In conjunction with `message_attribute` arg, will
            display a unique property of the first item in the current page.
          LAST_ITEM_MARKER   : In conjunction with `message_attribute` arg, will
            display a unique property of the last item in the current page.
    message_attribute: String or list, the name of a signature field within a
    single returned item which identifies that unique item. This field is used
    with `page_message` to templatize a paging status message.
    soft_errors: Bool, If True, writes non-fatal errors to stderr.
    throw_reasons: A list of Google HTTP error reason strings indicating the
      errors generated by this request should be re-thrown. All other HTTP
      errors are consumed.
    retry_reasons: A list of Google HTTP error reason strings indicating which
      error should be retried, using exponential backoff techniques, when the
      error reason is encountered.
    page_args_in_body: Some APIs like Chrome Policy want pageToken and pageSize
      in the body.
    **kwargs: Additional params to pass to the request method.

  Returns:
    A list of all items received from all paged responses.
  """
    if page_args_in_body:
        kwargs.setdefault('body', {})
    if 'maxResults' not in kwargs and 'pageSize' not in kwargs and 'pageSize' not in kwargs.get('body', {}):
        page_key = _get_max_page_size_for_api_call(service, function, **kwargs)
        if page_key:
            if page_args_in_body:
                kwargs['body'].update(page_key)
            else:
                kwargs.update(page_key)
    all_items = []
    page_token = None
    total_items = 0
    while True:
        page = call(service,
                    function,
                    soft_errors=soft_errors,
                    throw_reasons=throw_reasons,
                    retry_reasons=retry_reasons,
                    **kwargs)
        page_token, total_items = process_page(page, items, all_items, total_items, page_message, message_attribute)
        if not page_token:
            finalize_page_message(page_message)
            if type(all_items) is not list:
                all_items = all_items.values()
            return all_items
        if page_args_in_body:
            kwargs['body']['pageToken'] = page_token
        else:
            kwargs['pageToken'] = page_token

# TODO: Make this private once all execution related items that use this method
# have been brought into this file
def handle_oauth_token_error(e, soft_errors):
    """On a token error, exits the application and writes a message to stderr.

  Args:
    e: google.auth.exceptions.RefreshError, The error to handle.
    soft_errors: Boolean, if True, suppresses any applicable errors and instead
      returns to the caller.
  """
    token_error = str(e).replace('.', '')
    if token_error in errors.OAUTH2_TOKEN_ERRORS or token_error.startswith(
            'Invalid response'):
        if soft_errors:
            return
        if not GM_Globals[GM_CURRENT_API_USER]:
            display.print_error(
                MESSAGE_API_ACCESS_DENIED.format(
                    GM_Globals[GM_OAUTH2SERVICE_ACCOUNT_CLIENT_ID],
                    ','.join(GM_Globals[GM_CURRENT_API_SCOPES])))
            controlflow.system_error_exit(12, MESSAGE_API_ACCESS_CONFIG)
        else:
            controlflow.system_error_exit(
                19,
                MESSAGE_SERVICE_NOT_APPLICABLE.format(
                    GM_Globals[GM_CURRENT_API_USER]))
    controlflow.system_error_exit(18, f'Authentication Token Error - {str(e)}')


def get_enum_values_minus_unspecified(values):
    return [a_type for a_type in values if '_UNSPECIFIED' not in a_type]
