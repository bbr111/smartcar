import asyncio
from functools import partial
from http import HTTPStatus
import json
import logging
from typing import Any

from aiohttp import ClientResponseError
from homeassistant.components import cloud, webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_TOKEN, CONF_WEBHOOK_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.config_entry_oauth2_flow import (
    async_get_config_entry_implementation,
)
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from . import util
from .auth import AbstractAuth
from .auth_impl import AccessTokenAuthImpl, AsyncConfigEntryAuth
from .const import API_HOST, CONF_CLOUDHOOK, DOMAIN, PLATFORMS, Scope
from .coordinator import SmartcarVehicleCoordinator
from .errors import EmptyVehicleListError, InvalidAuthError, MissingVINError
from .services import async_setup_services
from .types import SmartcarData
from .webhooks import handle_webhook, webhook_url_from_id

_LOGGER = logging.getLogger(__name__)


async def async_setup(  # noqa: RUF029
    hass: HomeAssistant,
    config: ConfigType,  # noqa: ARG001
) -> bool:
    """Set up Smartcar services.

    Returns:
        If the setup was successful.
    """
    async_setup_services(hass)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Smartcar from a config entry.

    Returns:
        If the setup was successful.

    Raises:
        ConfigEntryError: For overlapping VIN in config entries.
    """
    implementation = await async_get_config_entry_implementation(hass, entry)
    websession = async_get_clientsession(hass)
    auth = AsyncConfigEntryAuth(
        websession, implementation.client_id, implementation.client_secret, API_HOST
    )
    coordinators: dict[str, SmartcarVehicleCoordinator] = {}
    meta_coordinator = DataUpdateCoordinator(
        hass, _LOGGER, name=f"{DOMAIN}_meta", config_entry=entry
    )
    meta_coordinator.async_set_updated_data({})
    entry.runtime_data = SmartcarData(
        auth=auth,
        coordinators=coordinators,
        meta_coordinator=meta_coordinator,
    )
    device_registry = dr.async_get(hass)
    other_vins = vehicle_vins_in_use(hass, entry)

    for vehicle_id, details in entry.data.get("vehicles", {}).items():
        vin = details["vin"]
        make = details.get("make")
        model = details.get("model")
        year = details.get("year")

        if vin in other_vins:
            msg = f"Cannot setup multiple config entries with VIN {vin}"
            raise ConfigEntryError(msg)

        # register device
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, vin)},
            manufacturer=make,
            model=f"{model} ({year})" if model and year else model,
            name=f"{make} {model}" if make and model else f"Smartcar {vin[-4:]}",
        )
        _LOGGER.info("Registered device for VIN: %s", vin)

        # create and store coordinator
        coordinator = SmartcarVehicleCoordinator(
            hass, auth, vehicle_id, vin, details.get("user_id", ""), entry
        )
        coordinators[vin] = coordinator
        _LOGGER.debug("Coordinator created and initial data fetched for VIN: %s", vin)

    # setup platforms before doing first refresh. this gets the entity registry
    # populated with the desired entities & allows the coordinator to determine
    # what to fetch on the first refresh. (some entities, for instance, are
    # disabled by default.)
    _LOGGER.debug("Forwarding setup to platforms: %s", PLATFORMS)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if CONF_WEBHOOK_ID in entry.data:
        _LOGGER.info(
            "Registering webhook at url: %s",
            (await webhook_url_from_id(hass, entry.data[CONF_WEBHOOK_ID]))[0],
        )
        webhook.async_register(
            hass,
            DOMAIN,
            entry.title,
            entry.data[CONF_WEBHOOK_ID],
            partial(handle_webhook, config_entry=entry),
        )
    else:
        _LOGGER.debug("Webhooks are not enabled")

    await asyncio.gather(
        *[async_do_first_refresh(coordinator) for coordinator in coordinators.values()]
    )

    # log stored scopes once on successful setup
    _LOGGER.info(
        "Using token with scopes: %s", entry.data.get("token", {}).get("scopes")
    )

    entry.async_on_unload(
        entry.add_update_listener(
            partial(async_update_listener, initial_data=entry.data)
        )
    )

    return True


async def async_do_first_refresh(coordinator: SmartcarVehicleCoordinator) -> None:
    await coordinator.async_config_entry_first_refresh()
    _LOGGER.debug(
        "Coordinator created and initial data fetched for VIN: %s", coordinator.vin
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry.

    Returns:
        If the unload was successful.
    """
    _LOGGER.info("Unloading Smartcar entry %s", entry.entry_id)
    if CONF_WEBHOOK_ID in entry.data:
        webhook.async_unregister(hass, entry.data[CONF_WEBHOOK_ID])
    return bool(await hass.config_entries.async_unload_platforms(entry, PLATFORMS))


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Cleanup when entry is removed."""
    if CONF_WEBHOOK_ID in entry.data and (
        cloud.async_active_subscription(hass) or entry.data.get(CONF_CLOUDHOOK, False)
    ):
        try:
            _LOGGER.debug(
                "Removing Smartcar cloudhook (%s)", entry.data[CONF_WEBHOOK_ID]
            )
            await cloud.async_delete_cloudhook(hass, entry.data[CONF_WEBHOOK_ID])
        except cloud.CloudNotAvailable:
            pass


async def async_update_listener(
    hass: HomeAssistant,
    entry: ConfigEntry,
    initial_data: dict[str, Any],
) -> None:
    """Handle options update."""

    entry_data = {k: v for k, v in entry.data.items() if k != "token"}
    initial_data = {k: v for k, v in initial_data.items() if k != "token"}

    if entry_data != initial_data:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    _LOGGER.debug(
        "Migrating configuration from version %s.%s",
        config_entry.version,
        config_entry.minor_version,
    )

    # prevent rollbacks
    if config_entry.version > 2:
        return False

    if config_entry.version == 1:
        old_data = config_entry.data
        session = async_get_clientsession(hass)
        token = old_data[CONF_TOKEN]
        access_token = token[CONF_ACCESS_TOKEN]
        scopes = token["scope"].split(" ")
        auth = AccessTokenAuthImpl(session, access_token, API_HOST)

        # copy old data & remove old keys
        new_data = {**old_data}
        new_data[CONF_TOKEN] = {**old_data[CONF_TOKEN]}
        new_data[CONF_TOKEN].pop("scope", None)

        await populate_entry_data(new_data, auth, scopes)

        old_vehicle_ids = set(old_data.get("vehicles", {}).keys())
        new_vehicle_ids = set(new_data["vehicles"].keys())

        # limit the vehicles in the config entry to whatever was in the previous
        # entry even if the API is returning new items.
        if old_vehicle_ids:
            for vehicle_id in new_vehicle_ids:
                if vehicle_id not in old_vehicle_ids:
                    new_data["vehicles"].pop(vehicle_id, None)

        # ensure all previously accessible vehicles are still accessible.
        inaccessible_vehicle_ids = [
            vehicle_id
            for vehicle_id in old_vehicle_ids
            if vehicle_id not in new_vehicle_ids
        ]

        if inaccessible_vehicle_ids:
            _LOGGER.error(
                "Vehicle(s) are no longer accessible via the API: %s",
                inaccessible_vehicle_ids,
            )
            return False

        hass.config_entries.async_update_entry(
            config_entry,
            unique_id=util.unique_id_from_entry_data(new_data),
            data=new_data,
            version=2,
            minor_version=0,
        )

    _LOGGER.debug(
        "Migration to configuration version %s.%s successful",
        config_entry.version,
        config_entry.minor_version,
    )

    return True


def vehicle_vins_in_use(
    hass: HomeAssistant, config_entry: ConfigEntry = None
) -> set[str]:
    return {
        vehicle["vin"]
        for other_entry in hass.config_entries.async_entries(DOMAIN)
        for vehicle in other_entry.data.get("vehicles", {}).values()
        if not config_entry or other_entry.unique_id != config_entry.unique_id
    }


async def populate_entry_data(
    data: dict,
    auth: AbstractAuth,
    scopes: list[Scope],
) -> None:
    """Populate config entry data during initial creation or migration."""
    _inject_requested_scopes_into_entry_data(data, scopes)

    await _store_all_vehicles(data, auth)


def _inject_requested_scopes_into_entry_data(data: dict, scopes: list[Scope]) -> None:
    """Inject selected scopes into stored token data."""
    data.setdefault("token", {})["scopes"] = scopes


async def _store_all_vehicles(
    data: dict,
    auth: AbstractAuth,
) -> None:
    """Fetch and store data for all Smartcar v3 connections.

    Raises:
        EmptyVehicleListError: If no vehicles are found.
        InvalidAuthError: If the request cannot be authorized.
        ClientResponseError: If there is a request error.
    """

    _LOGGER.info("Fetching Smartcar vehicle connections...")

    data["vehicles"] = {}

    try:
        connections_resp = await auth.request("get", "connections")
        connections_resp.raise_for_status()
        connections_data = await connections_resp.json()
        _LOGGER.debug("Smartcar /connections response: %s", connections_data)
        connections = (
            connections_data.get("connections")
            or connections_data.get("vehicles")
            or connections_data.get("data")
            or []
        )
    except ClientResponseError as err:
        if err.status == HTTPStatus.UNAUTHORIZED:
            msg = f"Auth error fetching vehicle connections: {err.status}"
            raise InvalidAuthError(msg) from err
        raise

    _LOGGER.info("Found %s vehicle connections", len(connections))

    if not connections:
        _LOGGER.warning(
            "Smartcar returned no vehicle connections; raw response: %s",
            connections_data,
        )
        raise EmptyVehicleListError

    await asyncio.gather(
        *[_store_vehicle_details(data, auth, connection) for connection in connections]
    )


def _find_vin(obj: Any) -> str | None:
    """Recursively search a decoded JSON structure for a VIN value.

    Returns:
        The first value of a key named ``vin``, or None.
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key.lower() == "vin" and isinstance(value, str) and value:
                return value
            found = _find_vin(value)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_vin(item)
            if found:
                return found
    return None


def _extract_vehicle_ids(payload: Any) -> list[str]:
    """Extract vehicle ids from a Smartcar vehicles-list response.

    Handles both the legacy ``{"vehicles": ["id", ...]}`` shape and a v3
    ``{"data": [{"id": "..."}]}`` shape.

    Returns:
        The list of vehicle ids found.
    """
    ids: list[str] = []
    if not isinstance(payload, dict):
        return ids

    for item in payload.get("vehicles") or []:
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, dict) and item.get("id"):
            ids.append(item["id"])

    for item in payload.get("data") or []:
        if isinstance(item, dict) and item.get("id"):
            ids.append(item["id"])

    return ids


async def _store_vehicle_details(
    data: dict,
    auth: AbstractAuth,
    connection: dict,
) -> None:
    """Fetch and store data for a single Smartcar v3 connection.

    Raises:
        MissingVINError: If the VIN is not available.
        InvalidAuthError: If the request cannot be authorized.
        ClientResponseError: If there is a request error.
    """

    relationships = connection.get("relationships", {})
    attributes = connection.get("attributes", {})

    vehicle_id = (
        connection.get("vehicleId")
        or connection.get("vehicle_id")
        or relationships.get("vehicle", {}).get("data", {}).get("id")
    )
    user_id = (
        connection.get("userId")
        or connection.get("user_id")
        or relationships.get("user", {}).get("data", {}).get("id")
        or attributes.get("user", {}).get("id")
    )

    if not vehicle_id or not user_id:
        msg = f"Incomplete Smartcar connection: {connection}"
        raise MissingVINError(msg)

    # v3 embeds make/model/year in the connection's vehicle attributes.
    vehicle_attrs = attributes.get("vehicle", {})
    connection_id = connection.get("id")

    # DIAGNOSTIC PROBE: the connection's vehicle id is rejected by the data API
    # (VEHICLE_NOT_FOUND) and /v3/vehicles is INVALID_PATH. Try several
    # candidate endpoints to discover which one returns the vehicle data, and
    # use the first that succeeds.
    # (label, path, params, headers, send_sc_user_id)
    # The Postman collection calls /vehicles/{id} WITHOUT sc-user-id, but
    # /vehicles/{id}/signals WITH it.
    probe_candidates: list[tuple[str, str, dict | None, dict | None, bool]] = [
        ("vehicle (no sc-user-id)", f"vehicles/{vehicle_id}", None, None, False),
        ("vehicle (sc-user-id)", f"vehicles/{vehicle_id}", None, None, True),
        (
            "vehicle+mode=live (no sc-user-id)",
            f"vehicles/{vehicle_id}",
            {"mode": "live"},
            None,
            False,
        ),
        ("connection-self", f"connections/{connection_id}", None, None, False),
        (
            "signals (sc-user-id)",
            f"vehicles/{vehicle_id}/signals",
            {"page[size]": 200},
            None,
            True,
        ),
        (
            "signals (no sc-user-id)",
            f"vehicles/{vehicle_id}/signals",
            {"page[size]": 200},
            None,
            False,
        ),
    ]

    working: tuple[str, dict] | None = None
    for label, path, params, headers, send_uid in probe_candidates:
        kwargs: dict[str, Any] = {}
        if send_uid:
            kwargs["user_id"] = user_id
        if params:
            kwargs["params"] = params
        if headers:
            kwargs["headers"] = headers
        try:
            resp = await auth.request("get", path, **kwargs)
            body = await resp.text()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("PROBE [%s] GET /v3/%s raised: %s", label, path, err)
            continue
        _LOGGER.warning(
            "PROBE [%s] GET /v3/%s -> %s: %s", label, path, resp.status, body[:800]
        )
        if resp.status == HTTPStatus.OK and working is None:
            try:
                working = (path, json.loads(body))
            except ValueError:
                pass

    if working is None:
        msg = (
            f"No working vehicle-data endpoint for connection {connection_id}; "
            "see PROBE log lines above"
        )
        raise MissingVINError(msg)

    used_path, vehicle_info = working
    _LOGGER.warning("Using vehicle-data endpoint /v3/%s", used_path)

    resource = vehicle_info.get("data", vehicle_info)
    if isinstance(resource, list):
        resource = resource[0] if resource else {}
    resource_attrs = (
        resource.get("attributes", resource) if isinstance(resource, dict) else {}
    )

    make = vehicle_attrs.get("make") or resource_attrs.get("make")
    model = vehicle_attrs.get("model") or resource_attrs.get("model")
    year = vehicle_attrs.get("year") or resource_attrs.get("year")

    vin = _find_vin(vehicle_info)
    if not vin:
        msg = f"No VIN found via /v3/{used_path}; see PROBE log lines above"
        raise MissingVINError(msg)

    data["vehicles"][vehicle_id] = {
        "vin": vin,
        "user_id": user_id,
        "make": make,
        "model": model,
        "year": year,
    }
