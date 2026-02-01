"""Support for monitoring plants."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.components.utility_meter.const import (
    DATA_TARIFF_SENSORS,
    DATA_UTILITY,
)
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_PICTURE,
    ATTR_ICON,
    ATTR_NAME,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_OK,
    STATE_PROBLEM,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    Platform,
)
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import (
    area_registry as ar,
)
from homeassistant.helpers import (
    config_validation as cv,
)
from homeassistant.helpers import (
    device_registry as dr,
)
from homeassistant.helpers import (
    entity_registry as er,
)
from homeassistant.helpers.entity import Entity, async_generate_entity_id
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.restore_state import RestoreEntity

from . import group
from .const import (
    ATTR_CONDUCTIVITY,
    ATTR_CURRENT,
    ATTR_DLI,
    ATTR_HUMIDITY,
    ATTR_ILLUMINANCE,
    ATTR_LAST_WATERED,
    ATTR_LIMITS,
    ATTR_MAX,
    ATTR_METERS,
    ATTR_MIN,
    ATTR_MOISTURE,
    ATTR_NEXT_WATERING,
    ATTR_OUTSIDE,
    ATTR_PLANT,
    ATTR_ROOM_HUMIDITY,
    ATTR_ROOM_TEMPERATURE,
    ATTR_SENSOR,
    ATTR_SENSORS,
    ATTR_SNOOZE_UNTIL,
    ATTR_SPECIES,
    ATTR_TEMPERATURE,
    ATTR_THRESHOLDS,
    ATTR_WATERING,
    ATTR_WEATHER_ENTITY,
    CONF_WATERING,
    DATA_SOURCE,
    DOMAIN,
    DOMAIN_PLANTBOOK,
    FLOW_CONDUCTIVITY_TRIGGER,
    FLOW_DLI_TRIGGER,
    FLOW_HUMIDITY_TRIGGER,
    FLOW_ILLUMINANCE_TRIGGER,
    FLOW_MOISTURE_TRIGGER,
    FLOW_NOTIFICATION_SERVICE,
    FLOW_OUTSIDE,
    FLOW_PLANT_INFO,
    FLOW_SENSOR_ROOM_HUMIDITY,
    FLOW_SENSOR_ROOM_TEMPERATURE,
    FLOW_TEMPERATURE_TRIGGER,
    FLOW_WEATHER_ENTITY,
    OPB_DISPLAY_PID,
    READING_CONDUCTIVITY,
    READING_DLI,
    READING_HUMIDITY,
    READING_ILLUMINANCE,
    READING_MOISTURE,
    READING_TEMPERATURE,
    SERVICE_REPLACE_SENSOR,
    SERVICE_SNOOZE,
    SERVICE_WATERED,
    STATE_HIGH,
    STATE_LOW,
)
from .plant_helpers import PlantHelper

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.NUMBER, Platform.SENSOR]

# Use this during testing to generate some dummy-sensors
# to provide random readings for temperature, moisture etc.
#
SETUP_DUMMY_SENSORS = False
USE_DUMMY_SENSORS = False

# Removed.
# Have not been used for a long time
#
# async def async_setup(hass: HomeAssistant, config: dict):
#     """
#     Set up the plant component
#
#     Configuration.yaml is no longer used.
#     This function only tries to migrate the legacy config.
#     """
#     if config.get(DOMAIN):
#         # Only import if we haven't before.
#         config_entry = _async_find_matching_config_entry(hass)
#         if not config_entry:
#             _LOGGER.debug("Old setup - with config: %s", config[DOMAIN])
#             for plant in config[DOMAIN]:
#                 if plant != DOMAIN_PLANTBOOK:
#                     _LOGGER.info("Migrating plant: %s", plant)
#                     await async_migrate_plant(hass, plant, config[DOMAIN][plant])
#         else:
#             _LOGGER.warning(
#                 "Config already imported. Please delete all your %s related config from configuration.yaml",
#                 DOMAIN,
#             )
#     return True


@callback
def _async_find_matching_config_entry(hass: HomeAssistant) -> ConfigEntry | None:
    """Check if there are migrated entities"""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.source == SOURCE_IMPORT:
            return entry


async def async_migrate_plant(hass: HomeAssistant, plant_id: str, config: dict) -> None:
    """Try to migrate the config from yaml"""

    if ATTR_NAME not in config:
        config[ATTR_NAME] = plant_id.replace("_", " ").capitalize()
    plant_helper = PlantHelper(hass)
    plant_config = await plant_helper.generate_configentry(config=config)
    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_IMPORT}, data=plant_config
        )
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Set up Plant from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    if FLOW_PLANT_INFO not in entry.data:
        return True

    hass.data[DOMAIN].setdefault(entry.entry_id, {})
    _LOGGER.debug("Setting up config entry %s: %s", entry.entry_id, entry)

    plant = PlantDevice(hass, entry)
    hass.data[DOMAIN][entry.entry_id][ATTR_PLANT] = plant

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    plant_entities = [
        plant,
    ]

    # Add all the entities to Hass
    component = EntityComponent(_LOGGER, DOMAIN, hass)
    await component.async_add_entities(plant_entities)

    # Add the rest of the entities to device registry together with plant
    device_id = plant.device_id
    await _plant_add_to_device_registry(hass, plant_entities, device_id)
    # await _plant_add_to_device_registry(hass, plant.integral_entities, device_id)
    # await _plant_add_to_device_registry(hass, plant.threshold_entities, device_id)
    # await _plant_add_to_device_registry(hass, plant.meter_entities, device_id)

    #
    # Set up utility sensor
    hass.data.setdefault(DATA_UTILITY, {})
    hass.data[DATA_UTILITY].setdefault(entry.entry_id, {})
    hass.data[DATA_UTILITY][entry.entry_id].setdefault(DATA_TARIFF_SENSORS, [])
    hass.data[DATA_UTILITY][entry.entry_id][DATA_TARIFF_SENSORS].append(plant.dli)

    #
    # Service call to replace sensors
    async def replace_sensor(call: ServiceCall) -> None:
        """Replace a sensor entity within a plant device"""
        meter_entity = call.data.get("meter_entity")
        new_sensor = call.data.get("new_sensor")
        found = False
        for entry_id in hass.data[DOMAIN]:
            if ATTR_SENSORS in hass.data[DOMAIN][entry_id]:
                for sensor in hass.data[DOMAIN][entry_id][ATTR_SENSORS]:
                    if sensor.entity_id == meter_entity:
                        found = True
                        break
        if not found:
            _LOGGER.warning(
                "Refuse to update non-%s entities: %s", DOMAIN, meter_entity
            )
            return False
        if new_sensor and new_sensor != "" and not new_sensor.startswith("sensor."):
            _LOGGER.warning("%s is not a sensor", new_sensor)
            return False

        try:
            meter = hass.states.get(meter_entity)
        except AttributeError:
            _LOGGER.error("Meter entity %s not found", meter_entity)
            return False
        if meter is None:
            _LOGGER.error("Meter entity %s not found", meter_entity)
            return False

        if new_sensor and new_sensor != "":
            try:
                test = hass.states.get(new_sensor)
            except AttributeError:
                _LOGGER.error("New sensor entity %s not found", meter_entity)
                return False
            if test is None:
                _LOGGER.error("New sensor entity %s not found", meter_entity)
                return False
        else:
            new_sensor = None

        _LOGGER.info(
            "Going to replace the external sensor for %s with %s",
            meter_entity,
            new_sensor,
        )
        for key in hass.data[DOMAIN]:
            if ATTR_SENSORS in hass.data[DOMAIN][key]:
                meters = hass.data[DOMAIN][key][ATTR_SENSORS]
                for meter in meters:
                    if meter.entity_id == meter_entity:
                        meter.replace_external_sensor(new_sensor)
        return

    hass.services.async_register(DOMAIN, SERVICE_REPLACE_SENSOR, replace_sensor)
    websocket_api.async_register_command(hass, ws_get_info)
    plant.async_schedule_update_ha_state(True)

    # Lets add the dummy sensors automatically if we are testing stuff
    if USE_DUMMY_SENSORS is True:
        for sensor in plant.meter_entities:
            if sensor.external_sensor is None:
                await hass.services.async_call(
                    domain=DOMAIN,
                    service=SERVICE_REPLACE_SENSOR,
                    service_data={
                        "meter_entity": sensor.entity_id,
                        "new_sensor": sensor.entity_id.replace(
                            "sensor.", "sensor.dummy_"
                        ),
                    },
                    blocking=False,
                    limit=30,
                )

    # Register services
    if not hass.services.has_service(DOMAIN, SERVICE_WATERED):

        async def watered(call: ServiceCall) -> None:
            """Service call to mark a plant as watered."""
            entity_ids = call.data.get("entity_id")
            if not entity_ids:
                return
            if isinstance(entity_ids, str):
                entity_ids = [entity_ids]

            _LOGGER.debug("Service watered called for %s", entity_ids)

            for entry_id in hass.data[DOMAIN]:
                if not isinstance(hass.data[DOMAIN][entry_id], dict):
                    continue
                plant_obj = hass.data[DOMAIN][entry_id].get(ATTR_PLANT)
                if plant_obj and plant_obj.entity_id in entity_ids:
                    _LOGGER.info("Marking %s as watered", plant_obj.entity_id)
                    plant_obj.async_watered()

        async def snooze(call: ServiceCall) -> None:
            """Service call to snooze watering notification."""
            entity_ids = call.data.get("entity_id")
            if not entity_ids:
                return
            if isinstance(entity_ids, str):
                entity_ids = [entity_ids]

            for entry_id in hass.data[DOMAIN]:
                if not isinstance(hass.data[DOMAIN][entry_id], dict):
                    continue
                plant_obj = hass.data[DOMAIN][entry_id].get(ATTR_PLANT)
                if plant_obj and plant_obj.entity_id in entity_ids:
                    plant_obj.async_snooze()

        hass.services.async_register(
            DOMAIN, SERVICE_WATERED, watered, schema=cv.make_entity_service_schema({})
        )
        hass.services.async_register(
            DOMAIN, SERVICE_SNOOZE, snooze, schema=cv.make_entity_service_schema({})
        )

    async def handle_notification_action(event) -> None:
        """Handle actionable notification events."""
        action = event.data.get("action")
        entity_id = event.data.get("entity_id")
        if action == "PLANT_WATERED":
            await hass.services.async_call(
                DOMAIN, SERVICE_WATERED, {"entity_id": entity_id}
            )
        elif action == "PLANT_SNOOZE":
            await hass.services.async_call(
                DOMAIN, SERVICE_SNOOZE, {"entity_id": entity_id}
            )

    hass.bus.async_listen("mobile_app_notification_action", handle_notification_action)

    return True


async def _plant_add_to_device_registry(
    hass: HomeAssistant, plant_entities: list[Entity], device_id: str
) -> None:
    """Add all related entities to the correct device_id"""

    # There must be a better way to do this, but I just can't find a way to set the
    # device_id when adding the entities.
    erreg = er.async_get(hass)
    for entity in plant_entities:
        erreg.async_update_entity(entity.registry_entry.entity_id, device_id=device_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        hass.data[DATA_UTILITY].pop(entry.entry_id)
        _LOGGER.info(hass.data[DOMAIN])
        for entry_id in list(hass.data[DOMAIN].keys()):
            if len(hass.data[DOMAIN][entry_id]) == 0:
                _LOGGER.info("Removing entry %s", entry_id)
                del hass.data[DOMAIN][entry_id]
        if len(hass.data[DOMAIN]) == 0:
            _LOGGER.info("Removing domain %s", DOMAIN)
            hass.services.async_remove(DOMAIN, SERVICE_REPLACE_SENSOR)
            del hass.data[DOMAIN]
    return unload_ok


@websocket_api.websocket_command(
    {
        vol.Required("type"): "plant/get_info",
        vol.Required("entity_id"): str,
    }
)
@callback
def ws_get_info(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Handle the websocket command."""
    # _LOGGER.debug("Got websocket request: %s", msg)

    if DOMAIN not in hass.data:
        connection.send_error(
            msg["id"], "domain_not_found", f"Domain {DOMAIN} not found"
        )
        return

    for key in hass.data[DOMAIN]:
        if not ATTR_PLANT in hass.data[DOMAIN][key]:
            continue
        plant_entity = hass.data[DOMAIN][key][ATTR_PLANT]
        if plant_entity.entity_id == msg["entity_id"]:
            # _LOGGER.debug("Sending websocket response: %s", plant_entity.websocket_info)
            try:
                connection.send_result(
                    msg["id"], {"result": plant_entity.websocket_info}
                )
            except ValueError as e:
                _LOGGER.warning(e)
            return
    connection.send_error(
        msg["id"], "entity_not_found", f"Entity {msg['entity_id']} not found"
    )
    return


class PlantDevice(RestoreEntity):
    """Base device for plants"""

    def __init__(self, hass: HomeAssistant, config: ConfigEntry) -> None:
        """Initialize the Plant component."""
        self._config = config
        self._hass = hass
        self._attr_name = config.data[FLOW_PLANT_INFO][ATTR_NAME]
        self._config_entries = []
        self._data_source = config.data[FLOW_PLANT_INFO].get(DATA_SOURCE)
        self.last_watered = None
        self.snooze_until = None
        self.last_notified = None

        # Get entity_picture from options or from initial config
        self._attr_entity_picture = self._config.options.get(
            ATTR_ENTITY_PICTURE,
            self._config.data[FLOW_PLANT_INFO].get(ATTR_ENTITY_PICTURE),
        )
        # Get species from options or from initial config
        self.species = self._config.options.get(
            ATTR_SPECIES, self._config.data[FLOW_PLANT_INFO].get(ATTR_SPECIES)
        )
        # Get display_species from options or from initial config
        self.display_species = (
            self._config.options.get(
                OPB_DISPLAY_PID, self._config.data[FLOW_PLANT_INFO].get(OPB_DISPLAY_PID)
            )
            or self.species
        )
        self.scientific_name = self._config.data[FLOW_PLANT_INFO].get("scientific_name")
        self.common_name = self._config.data[FLOW_PLANT_INFO].get("common_name")
        self.category = self._config.data[FLOW_PLANT_INFO].get("category")
        self.origin = self._config.data[FLOW_PLANT_INFO].get("origin")

        self._attr_unique_id = self._config.entry_id

        self.entity_id = async_generate_entity_id(
            f"{DOMAIN}.{{}}", self.name, current_ids={}
        )

        self.plant_complete = False
        self._device_id = None

        self._check_days = None

        self.max_moisture = None
        self.min_moisture = None
        self.max_temperature = None
        self.min_temperature = None
        self.max_conductivity = None
        self.min_conductivity = None
        self.max_illuminance = None
        self.min_illuminance = None
        self.max_humidity = None
        self.min_humidity = None
        self.max_dli = None
        self.min_dli = None

        self.sensor_moisture = None
        self.sensor_temperature = None
        self.sensor_conductivity = None
        self.sensor_illuminance = None
        self.sensor_humidity = None

        self.room_temperature_sensor = self._config.options.get(
            FLOW_SENSOR_ROOM_TEMPERATURE,
            config.data[FLOW_PLANT_INFO].get(FLOW_SENSOR_ROOM_TEMPERATURE),
        )
        self.room_humidity_sensor = self._config.options.get(
            FLOW_SENSOR_ROOM_HUMIDITY,
            config.data[FLOW_PLANT_INFO].get(FLOW_SENSOR_ROOM_HUMIDITY),
        )
        self.weather_entity = self._config.options.get(
            FLOW_WEATHER_ENTITY, config.data[FLOW_PLANT_INFO].get(FLOW_WEATHER_ENTITY)
        )
        self.notification_service = self._config.options.get(
            FLOW_NOTIFICATION_SERVICE,
            config.data[FLOW_PLANT_INFO].get(FLOW_NOTIFICATION_SERVICE),
        )
        self.watering_days = self._config.options.get(
            CONF_WATERING, config.data[FLOW_PLANT_INFO].get(CONF_WATERING, 7)
        )
        self.outside = self._config.options.get(
            FLOW_OUTSIDE, config.data[FLOW_PLANT_INFO].get(FLOW_OUTSIDE, False)
        )
        if not isinstance(self.watering_days, (int, float)):
            try:
                if isinstance(self.watering_days, str):
                    self.watering_days = float(self.watering_days.split(" ")[0])
                else:
                    self.watering_days = float(self.watering_days)
            except (ValueError, TypeError, IndexError):
                self.watering_days = 7
        self.next_watering = "0 j"
        self.watering_explanation = ""

        self.dli = None
        self.micro_dli = None
        self.ppfd = None
        self.total_integral = None

        self.conductivity_status = None
        self.illuminance_status = None
        self.moisture_status = None
        self.temperature_status = None
        self.humidity_status = None
        self.dli_status = None

    @property
    def entity_category(self) -> None:
        """The plant device itself does not have a category"""
        return None

    @property
    def device_class(self):
        return DOMAIN

    @property
    def device_id(self) -> str:
        """The device ID used for all the entities"""
        return self._device_id

    @property
    def device_info(self) -> dict:
        """Device info for devices"""
        return {
            "identifiers": {(DOMAIN, self.unique_id)},
            "name": self.name,
            "config_entries": self._config_entries,
            "model": self.display_species,
            "manufacturer": self.data_source,
        }

    @property
    def illuminance_trigger(self) -> bool:
        """Whether we will generate alarms based on illuminance"""
        return self._config.options.get(FLOW_ILLUMINANCE_TRIGGER, True)

    @property
    def humidity_trigger(self) -> bool:
        """Whether we will generate alarms based on humidity"""
        return self._config.options.get(FLOW_HUMIDITY_TRIGGER, True)

    @property
    def temperature_trigger(self) -> bool:
        """Whether we will generate alarms based on temperature"""
        return self._config.options.get(FLOW_TEMPERATURE_TRIGGER, True)

    @property
    def dli_trigger(self) -> bool:
        """Whether we will generate alarms based on dli"""
        return self._config.options.get(FLOW_DLI_TRIGGER, True)

    @property
    def moisture_trigger(self) -> bool:
        """Whether we will generate alarms based on moisture"""
        return self._config.options.get(FLOW_MOISTURE_TRIGGER, True)

    @property
    def conductivity_trigger(self) -> bool:
        """Whether we will generate alarms based on conductivity"""
        return self._config.options.get(FLOW_CONDUCTIVITY_TRIGGER, True)

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        await super().async_added_to_hass()
        self.update_registry()
        state = await self.async_get_last_state()
        if state:
            self.last_watered = state.attributes.get(ATTR_LAST_WATERED)
            self.snooze_until = state.attributes.get(ATTR_SNOOZE_UNTIL)
            self.last_notified = state.attributes.get("last_notified")

        if (
            not self.scientific_name
            or self.scientific_name == ""
            or not self.origin
            or self.origin == ""
            or not self.category
            or self.category == ""
        ):
            _LOGGER.debug("Refreshing OPB metadata for %s", self.name)
            plant_helper = PlantHelper(self._hass)
            opb_plant = await plant_helper.openplantbook_get(self.species)
            if opb_plant:
                self.scientific_name = opb_plant.get(
                    "scientific_name"
                ) or opb_plant.get("species")

                common_names = opb_plant.get("common_names") or opb_plant.get(
                    "common_name"
                )
                if isinstance(common_names, list):
                    names = []
                    for x in common_names:
                        if isinstance(x, dict):
                            names.append(
                                str(
                                    x.get(
                                        "name",
                                        x.get(
                                            "value", list(x.values())[0] if x else ""
                                        ),
                                    )
                                )
                            )
                        else:
                            names.append(str(x))
                    self.common_name = ", ".join([n for n in names if n])
                else:
                    self.common_name = common_names

                category = (
                    opb_plant.get("category")
                    or opb_plant.get("plant_type")
                    or opb_plant.get("type")
                )
                if isinstance(category, list):
                    self.category = ", ".join([str(x) for x in category if x])
                else:
                    self.category = category

                origins = (
                    opb_plant.get("origin")
                    or opb_plant.get("native_location")
                    or opb_plant.get("native_distribution")
                    or opb_plant.get("native_range")
                    or opb_plant.get("distribution")
                    or opb_plant.get("native_region")
                )

                if isinstance(origins, list):
                    origin_list = []
                    for x in origins:
                        if isinstance(x, dict):
                            origin_list.append(
                                str(
                                    x.get(
                                        "name",
                                        x.get(
                                            "value", list(x.values())[0] if x else ""
                                        ),
                                    )
                                )
                            )
                        else:
                            origin_list.append(str(x))
                    self.origin = ", ".join([o for o in origin_list if o])
                else:
                    self.origin = origins

                self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict:
        """Return the device specific state attributes."""
        attributes = {
            ATTR_SPECIES: self.display_species,
            f"{ATTR_SPECIES}_original": self.species,
            "scientific_name": self.scientific_name,
            "common_name": self.common_name,
            "category": self.category,
            "origin": self.origin,
            "pid": self.species,
            f"{ATTR_MOISTURE}_status": self.moisture_status,
            f"{ATTR_TEMPERATURE}_status": self.temperature_status,
            f"{ATTR_CONDUCTIVITY}_status": self.conductivity_status,
            f"{ATTR_ILLUMINANCE}_status": self.illuminance_status,
            f"{ATTR_HUMIDITY}_status": self.humidity_status,
            f"{ATTR_DLI}_status": self.dli_status,
            ATTR_NEXT_WATERING: self.next_watering,
            "next_watering_days": int(str(self.next_watering).split(" ")[0])
            if self.next_watering
            else 0,
            ATTR_LAST_WATERED: self.last_watered,
            ATTR_SNOOZE_UNTIL: self.snooze_until,
            "last_notified": self.last_notified,
            "watering_explanation": self.watering_explanation,
        }

        # Area lookup
        entity_registry = er.async_get(self._hass)
        entry = entity_registry.async_get(self.entity_id)
        if entry:
            area_id = entry.area_id
            if not area_id and entry.device_id:
                device_registry = dr.async_get(self._hass)
                device = device_registry.async_get(entry.device_id)
                if device:
                    area_id = device.area_id

            if area_id:
                area_registry = ar.async_get(self._hass)
                area = area_registry.async_get_area(area_id)
                if area:
                    attributes["area"] = area.name

        return attributes

    @property
    def websocket_info(self) -> dict:
        """Wesocket response"""

        # Fallback logic for temperature
        temp_val = STATE_UNAVAILABLE
        temp_icon = "mdi:thermometer"
        temp_unit = "°C"
        temp_sensor = None

        if self.sensor_temperature:
            temp_state = self._hass.states.get(self.sensor_temperature.entity_id)
            temp_val = getattr(temp_state, "state", STATE_UNAVAILABLE)
            temp_icon = self.sensor_temperature.icon
            temp_unit = self.sensor_temperature.unit_of_measurement
            temp_sensor = self.sensor_temperature.entity_id

        if (
            temp_val == STATE_UNKNOWN or temp_val == STATE_UNAVAILABLE
        ) and self.room_temperature_sensor:
            room_temp_state = self._hass.states.get(self.room_temperature_sensor)
            if room_temp_state:
                temp_val = room_temp_state.state
                temp_icon = room_temp_state.attributes.get("icon", temp_icon)
                temp_unit = room_temp_state.attributes.get(
                    "unit_of_measurement", temp_unit
                )
                temp_sensor = self.room_temperature_sensor

        # Fallback logic for humidity
        hum_val = STATE_UNAVAILABLE
        hum_icon = "mdi:water-percent"
        hum_unit = "%"
        hum_sensor = None

        if self.sensor_humidity:
            hum_state = self._hass.states.get(self.sensor_humidity.entity_id)
            hum_val = getattr(hum_state, "state", STATE_UNAVAILABLE)
            hum_icon = self.sensor_humidity.icon
            hum_unit = self.sensor_humidity.unit_of_measurement
            hum_sensor = self.sensor_humidity.entity_id

        if (
            hum_val == STATE_UNKNOWN or hum_val == STATE_UNAVAILABLE
        ) and self.room_humidity_sensor:
            room_hum_state = self._hass.states.get(self.room_humidity_sensor)
            if room_hum_state:
                hum_val = room_hum_state.state
                hum_icon = room_hum_state.attributes.get("icon", hum_icon)
                hum_unit = room_hum_state.attributes.get(
                    "unit_of_measurement", hum_unit
                )
                hum_sensor = self.room_humidity_sensor

        response = {
            ATTR_TEMPERATURE: {
                ATTR_MAX: getattr(self.max_temperature, "state", 40)
                if temp_sensor
                else None,
                ATTR_MIN: getattr(self.min_temperature, "state", 10)
                if temp_sensor
                else None,
                ATTR_CURRENT: temp_val,
                ATTR_ICON: temp_icon,
                ATTR_UNIT_OF_MEASUREMENT: temp_unit,
                ATTR_SENSOR: temp_sensor,
            },
            ATTR_ILLUMINANCE: {
                ATTR_MAX: getattr(self.max_illuminance, "state", 100000)
                if self.sensor_illuminance
                else None,
                ATTR_MIN: getattr(self.min_illuminance, "state", 0)
                if self.sensor_illuminance
                else None,
                ATTR_CURRENT: getattr(
                    self.sensor_illuminance, "state", STATE_UNAVAILABLE
                )
                if self.sensor_illuminance
                else STATE_UNAVAILABLE,
                ATTR_ICON: getattr(self.sensor_illuminance, "icon", "mdi:brightness-6"),
                ATTR_UNIT_OF_MEASUREMENT: getattr(
                    self.sensor_illuminance, "unit_of_measurement", "lx"
                ),
                ATTR_SENSOR: getattr(self.sensor_illuminance, "entity_id", None),
            },
            ATTR_MOISTURE: {
                ATTR_MAX: getattr(self.max_moisture, "state", 60)
                if self.sensor_moisture
                else None,
                ATTR_MIN: getattr(self.min_moisture, "state", 20)
                if self.sensor_moisture
                else None,
                ATTR_CURRENT: getattr(self.sensor_moisture, "state", STATE_UNAVAILABLE)
                if self.sensor_moisture
                else STATE_UNAVAILABLE,
                ATTR_ICON: getattr(self.sensor_moisture, "icon", "mdi:water"),
                ATTR_UNIT_OF_MEASUREMENT: getattr(
                    self.sensor_moisture, "unit_of_measurement", "%"
                ),
                ATTR_SENSOR: getattr(self.sensor_moisture, "entity_id", None),
            },
            ATTR_CONDUCTIVITY: {
                ATTR_MAX: getattr(self.max_conductivity, "state", 3000)
                if self.sensor_conductivity
                else None,
                ATTR_MIN: getattr(self.min_conductivity, "state", 500)
                if self.sensor_conductivity
                else None,
                ATTR_CURRENT: getattr(
                    self.sensor_conductivity, "state", STATE_UNAVAILABLE
                )
                if self.sensor_conductivity
                else STATE_UNAVAILABLE,
                ATTR_ICON: getattr(self.sensor_conductivity, "icon", "mdi:spa-outline"),
                ATTR_UNIT_OF_MEASUREMENT: getattr(
                    self.sensor_conductivity, "unit_of_measurement", "μS/cm"
                ),
                ATTR_SENSOR: getattr(self.sensor_conductivity, "entity_id", None),
            },
            ATTR_HUMIDITY: {
                ATTR_MAX: getattr(self.max_humidity, "state", 60)
                if hum_sensor
                else None,
                ATTR_MIN: getattr(self.min_humidity, "state", 20)
                if hum_sensor
                else None,
                ATTR_CURRENT: hum_val,
                ATTR_ICON: hum_icon,
                ATTR_UNIT_OF_MEASUREMENT: hum_unit,
                ATTR_SENSOR: hum_sensor,
            },
            ATTR_DLI: {
                ATTR_MAX: getattr(self.max_dli, "state", 30) if self.dli else None,
                ATTR_MIN: getattr(self.min_dli, "state", 2) if self.dli else None,
                ATTR_CURRENT: STATE_UNAVAILABLE,
                ATTR_ICON: getattr(self.dli, "icon", "mdi:counter")
                if self.dli
                else "mdi:counter",
                ATTR_UNIT_OF_MEASUREMENT: getattr(
                    self.dli, "unit_of_measurement", "mol/d⋅m²"
                )
                if self.dli
                else "mol/d⋅m²",
                ATTR_SENSOR: getattr(self.dli, "entity_id", None),
            },
            ATTR_NEXT_WATERING: self.next_watering,
            ATTR_LAST_WATERED: self.last_watered,
            ATTR_SNOOZE_UNTIL: self.snooze_until,
            ATTR_ROOM_TEMPERATURE: self.room_temperature_sensor,
            ATTR_ROOM_HUMIDITY: self.room_humidity_sensor,
            ATTR_WEATHER_ENTITY: self.weather_entity,
            ATTR_OUTSIDE: self.outside,
            ATTR_WATERING: self.watering_days,
            "area": None,
            "scientific_name": self.scientific_name,
            "common_name": self.common_name,
            "category": self.category,
            "origin": self.origin,
            "pid": self.species,
        }
        if self.dli and self.dli.state and self.dli.state != STATE_UNKNOWN:
            response[ATTR_DLI][ATTR_CURRENT] = float(self.dli.state)

        # Area lookup
        entity_registry = er.async_get(self._hass)
        entry = entity_registry.async_get(self.entity_id)
        if entry:
            area_id = entry.area_id
            if not area_id and entry.device_id:
                device_registry = dr.async_get(self._hass)
                device = device_registry.async_get(entry.device_id)
                if device:
                    area_id = device.area_id

            if area_id:
                area_registry = ar.async_get(self._hass)
                area = area_registry.async_get_area(area_id)
                if area:
                    response["area"] = area.name

        return response

    @property
    def threshold_entities(self) -> list[Entity]:
        """List all threshold entities"""
        return [
            self.max_conductivity,
            self.max_dli,
            self.max_humidity,
            self.max_illuminance,
            self.max_moisture,
            self.max_temperature,
            self.min_conductivity,
            self.min_dli,
            self.min_humidity,
            self.min_illuminance,
            self.min_moisture,
            self.min_temperature,
        ]

    @property
    def meter_entities(self) -> list[Entity]:
        """List all meter (sensor) entities"""
        return [
            self.sensor_conductivity,
            self.sensor_humidity,
            self.sensor_illuminance,
            self.sensor_moisture,
            self.sensor_temperature,
        ]

    @property
    def integral_entities(self) -> list(Entity):
        """List all integral entities"""
        return [
            self.dli,
            self.ppfd,
            self.total_integral,
        ]

    def add_image(self, image_url: str | None) -> None:
        """Set new entity_picture"""
        self._attr_entity_picture = image_url
        options = self._config.options.copy()
        options[ATTR_ENTITY_PICTURE] = image_url
        self._hass.config_entries.async_update_entry(self._config, options=options)

    def add_species(self, species: Entity | None) -> None:
        """Set new species"""
        self.species = species

    def add_thresholds(
        self,
        max_moisture: Entity | None,
        min_moisture: Entity | None,
        max_temperature: Entity | None,
        min_temperature: Entity | None,
        max_conductivity: Entity | None,
        min_conductivity: Entity | None,
        max_illuminance: Entity | None,
        min_illuminance: Entity | None,
        max_humidity: Entity | None,
        min_humidity: Entity | None,
        max_dli: Entity | None,
        min_dli: Entity | None,
    ) -> None:
        """Add the threshold entities"""
        self.max_moisture = max_moisture
        self.min_moisture = min_moisture
        self.max_temperature = max_temperature
        self.min_temperature = min_temperature
        self.max_conductivity = max_conductivity
        self.min_conductivity = min_conductivity
        self.max_illuminance = max_illuminance
        self.min_illuminance = min_illuminance
        self.max_humidity = max_humidity
        self.min_humidity = min_humidity
        self.max_dli = max_dli
        self.min_dli = min_dli

    def add_sensors(
        self,
        moisture: Entity | None,
        temperature: Entity | None,
        conductivity: Entity | None,
        illuminance: Entity | None,
        humidity: Entity | None,
    ) -> None:
        """Add the sensor entities"""
        self.sensor_moisture = moisture
        self.sensor_temperature = temperature
        self.sensor_conductivity = conductivity
        self.sensor_illuminance = illuminance
        self.sensor_humidity = humidity

    def add_dli(
        self,
        dli: Entity | None,
    ) -> None:
        """Add the DLI-utility sensors"""
        self.dli = dli
        self.plant_complete = True

    def add_calculations(self, ppfd: Entity, total_integral: Entity) -> None:
        """Add the intermediate calculation entities"""
        self.ppfd = ppfd
        self.total_integral = total_integral

    def update(self) -> None:
        """Run on every update of the entities"""

        new_state = STATE_OK
        known_state = False
        temperature = None
        humidity = None

        if self.sensor_moisture is not None:
            moisture = getattr(
                self._hass.states.get(self.sensor_moisture.entity_id), "state", None
            )
            if (
                moisture is not None
                and moisture != STATE_UNKNOWN
                and moisture != STATE_UNAVAILABLE
            ):
                known_state = True
                if float(moisture) < float(self.min_moisture.state):
                    self.moisture_status = STATE_LOW
                    if self.moisture_trigger:
                        new_state = STATE_PROBLEM
                elif float(moisture) > float(self.max_moisture.state):
                    self.moisture_status = STATE_HIGH
                    if self.moisture_trigger:
                        new_state = STATE_PROBLEM
                else:
                    self.moisture_status = STATE_OK

        if self.sensor_conductivity is not None:
            conductivity = getattr(
                self._hass.states.get(self.sensor_conductivity.entity_id), "state", None
            )
            if (
                conductivity is not None
                and conductivity != STATE_UNKNOWN
                and conductivity != STATE_UNAVAILABLE
            ):
                known_state = True
                if float(conductivity) < float(self.min_conductivity.state):
                    self.conductivity_status = STATE_LOW
                    if self.conductivity_trigger:
                        new_state = STATE_PROBLEM
                elif float(conductivity) > float(self.max_conductivity.state):
                    self.conductivity_status = STATE_HIGH
                    if self.conductivity_trigger:
                        new_state = STATE_PROBLEM
                else:
                    self.conductivity_status = STATE_OK

        if self.sensor_temperature is not None or self.room_temperature_sensor:
            temperature_state = None
            if self.sensor_temperature:
                temperature_state = self._hass.states.get(
                    self.sensor_temperature.entity_id
                )
            temperature = getattr(temperature_state, "state", None)

            if (
                temperature is None
                or temperature == STATE_UNKNOWN
                or temperature == STATE_UNAVAILABLE
            ):
                if self.room_temperature_sensor:
                    temperature_state = self._hass.states.get(
                        self.room_temperature_sensor
                    )
                    temperature = getattr(temperature_state, "state", None)

            if (
                temperature is not None
                and temperature != STATE_UNKNOWN
                and temperature != STATE_UNAVAILABLE
            ):
                known_state = True
                if self.min_temperature and float(temperature) < float(
                    self.min_temperature.state
                ):
                    self.temperature_status = STATE_LOW
                    if self.temperature_trigger:
                        new_state = STATE_PROBLEM
                elif self.max_temperature and float(temperature) > float(
                    self.max_temperature.state
                ):
                    self.temperature_status = STATE_HIGH
                    if self.temperature_trigger:
                        new_state = STATE_PROBLEM
                else:
                    self.temperature_status = STATE_OK

        if self.sensor_humidity is not None or self.room_humidity_sensor:
            humidity_state = None
            if self.sensor_humidity:
                humidity_state = self._hass.states.get(self.sensor_humidity.entity_id)
            humidity = getattr(humidity_state, "state", None)

            if (
                humidity is None
                or humidity == STATE_UNKNOWN
                or humidity == STATE_UNAVAILABLE
            ):
                if self.room_humidity_sensor:
                    humidity_state = self._hass.states.get(self.room_humidity_sensor)
                    humidity = getattr(humidity_state, "state", None)

            if (
                humidity is not None
                and humidity != STATE_UNKNOWN
                and humidity != STATE_UNAVAILABLE
            ):
                known_state = True
                if self.min_humidity and float(humidity) < float(
                    self.min_humidity.state
                ):
                    self.humidity_status = STATE_LOW
                    if self.humidity_trigger:
                        new_state = STATE_PROBLEM
                elif self.max_humidity and float(humidity) > float(
                    self.max_humidity.state
                ):
                    self.humidity_status = STATE_HIGH
                    if self.humidity_trigger:
                        new_state = STATE_PROBLEM
                else:
                    self.humidity_status = STATE_OK

        # Check the instant values for illuminance against "max"
        # Ignoring "min" value for illuminance as it would probably trigger every night
        if self.sensor_illuminance is not None:
            illuminance = getattr(
                self._hass.states.get(self.sensor_illuminance.entity_id), "state", None
            )
            if (
                illuminance is not None
                and illuminance != STATE_UNKNOWN
                and illuminance != STATE_UNAVAILABLE
            ):
                known_state = True
                if float(illuminance) > float(self.max_illuminance.state):
                    self.illuminance_status = STATE_HIGH
                    if self.illuminance_trigger:
                        new_state = STATE_PROBLEM
                else:
                    self.illuminance_status = STATE_OK

        # - Checking Low values would create "problem" every night...
        # Check DLI from the previous day against max/min DLI
        if (
            self.dli is not None
            and self.dli.native_value != STATE_UNKNOWN
            and self.dli.native_value != STATE_UNAVAILABLE
            and self.dli.state is not None
        ):
            known_state = True
            if float(self.dli.extra_state_attributes["last_period"]) > 0 and float(
                self.dli.extra_state_attributes["last_period"]
            ) < float(self.min_dli.state):
                self.dli_status = STATE_LOW
                if self.dli_trigger:
                    new_state = STATE_PROBLEM
            elif float(self.dli.extra_state_attributes["last_period"]) > 0 and float(
                self.dli.extra_state_attributes["last_period"]
            ) > float(self.max_dli.state):
                self.dli_status = STATE_HIGH
                if self.dli_trigger:
                    new_state = STATE_PROBLEM
            else:
                self.dli_status = STATE_OK

        # Calculate Next Watering
        days = 0
        explanation_lines = []
        base_days = self.watering_days or 7
        explanation_lines.append(f"Délai de base : {base_days} jours")

        if self.sensor_moisture is not None:
            moisture_state = self._hass.states.get(self.sensor_moisture.entity_id)
            if (
                moisture_state
                and moisture_state.state != STATE_UNKNOWN
                and moisture_state.state != STATE_UNAVAILABLE
            ):
                current_moisture = float(moisture_state.state)
                min_moisture = float(self.min_moisture.state)
                max_moisture = float(self.max_moisture.state)

                # Default loss rate: assumes self.watering_days to go from max to min
                daily_loss = (max_moisture - min_moisture) / base_days
                if daily_loss <= 0:
                    daily_loss = 5

                adj = 1.0
                # Use temperature if available (set above in the health check)
                try:
                    temp = float(temperature)
                    if temp != 22:
                        temp_adj = (temp - 22) * 0.05
                        adj *= 1 + temp_adj
                        explanation_lines.append(
                            f"Température ({temp}°C) : {'+' if temp_adj > 0 else ''}{int(temp_adj * 100)}% d'évaporation"
                        )
                except (ValueError, TypeError, NameError):
                    temp = 22

                # Use humidity if available
                try:
                    hum = float(humidity)
                    if hum != 50:
                        hum_adj = (hum - 50) * 0.004
                        adj *= 1 - hum_adj
                        explanation_lines.append(
                            f"Humidité ({hum}%) : {'-' if hum_adj > 0 else '+'}{abs(int(hum_adj * 100))}% d'évaporation"
                        )
                except (ValueError, TypeError, NameError):
                    hum = 50

                # Weather info
                if self.weather_entity:
                    weather_state = self._hass.states.get(self.weather_entity)
                    if weather_state:
                        forecast = weather_state.attributes.get("forecast", [])
                        if forecast:
                            rainy = any(
                                f.get("condition")
                                in ("rainy", "pouring", "hail", "snowy")
                                or f.get("precipitation", 0) > 2
                                for f in forecast[:2]
                            )
                            if rainy:
                                adj *= 0.5
                                explanation_lines.append(
                                    "Pluie prévue : -50% d'évaporation"
                                )

                adj = max(0.1, adj)
                actual_loss = daily_loss * adj
                days = int((current_moisture - min_moisture) / actual_loss)
                explanation_lines.append(
                    f"Consommation actuelle : {actual_loss:.1f}% / jour"
                )

        # If recently watered, or if no sensor, use timer-based logic
        if self.last_watered:
            try:
                last_watered_dt = datetime.fromisoformat(self.last_watered)
                time_since_watering = datetime.now() - last_watered_dt

                if time_since_watering < timedelta(hours=12):
                    # Force a refresh to full period if just watered (sensors might be slow)
                    if days < base_days:
                        days = base_days
                        explanation_lines.append(
                            "Arrosage récent détecté : réinitialisation du délai"
                        )
                elif self.sensor_moisture is None:
                    # No sensor? Use days since last watering
                    days = max(0, base_days - time_since_watering.days)
                    explanation_lines.append(
                        f"Calcul basé sur le temps ({time_since_watering.days}j écoulés)"
                    )
            except (ValueError, TypeError):
                pass

        if days <= 0:
            self.next_watering = "0 j"
        else:
            self.next_watering = f"{days} j"

        self.watering_explanation = "\n".join(explanation_lines)

        if not known_state:
            new_state = STATE_UNKNOWN

        self._attr_state = new_state
        self._check_and_notify()
        self.update_registry()

    @property
    def data_source(self) -> str | None:
        """Currently unused. For future use"""
        return None

    def update_registry(self) -> None:
        """Update registry with correct data"""
        # Is there a better way to add an entity to the device registry?

        device_registry = dr.async_get(self._hass)
        device_registry.async_get_or_create(
            config_entry_id=self._config.entry_id,
            identifiers={(DOMAIN, self.unique_id)},
            name=self.name,
            model=self.display_species,
            manufacturer=self.data_source,
        )
        if self._device_id is None:
            device = device_registry.async_get_device(
                identifiers={(DOMAIN, self.unique_id)}
            )
            self._device_id = device.id

    @callback
    def async_watered(self) -> None:
        """Mark the plant as watered."""
        self.last_watered = datetime.now().isoformat()
        self.snooze_until = None
        self.update()
        self.async_write_ha_state()

    @callback
    def async_snooze(self) -> None:
        """Snooze the watering notification."""
        self.snooze_until = (datetime.now() + timedelta(hours=1)).isoformat()
        self.update()
        self.async_write_ha_state()

    def _check_and_notify(self) -> None:
        """Check if we should send a notification."""
        if self.moisture_status != STATE_LOW or not self.moisture_trigger:
            return

        now = datetime.now()
        if self.snooze_until:
            try:
                snooze_dt = datetime.fromisoformat(self.snooze_until)
                if now < snooze_dt:
                    return
            except ValueError:
                pass

        if self.last_notified:
            try:
                last_dt = datetime.fromisoformat(self.last_notified)
                if now < last_dt + timedelta(hours=4):
                    return
            except ValueError:
                pass

        self._hass.async_create_task(self._async_send_notification())

    async def _async_send_notification(self) -> None:
        """Send a notification."""
        notify_service = self.notification_service or "all_phones"
        if not self._hass.services.has_service("notify", notify_service):
            return

        moisture = "???"
        if self.sensor_moisture:
            moisture = self.sensor_moisture.state

        service_data = {
            "title": f"Arrosage nécessaire : {self.name}",
            "message": f"Votre {self.display_species} a soif ! (Humidité : {moisture}%)",
            "data": {
                "tag": f"plant_watering_{self.entity_id}",
                "actions": [
                    {
                        "action": "PLANT_WATERED",
                        "title": "Arrosé",
                        "entity_id": self.entity_id,
                    },
                    {
                        "action": "PLANT_SNOOZE",
                        "title": "Snooze 1h",
                        "entity_id": self.entity_id,
                    },
                ],
            },
        }
        await self._hass.services.async_call("notify", notify_service, service_data)
        self.last_notified = datetime.now().isoformat()
        self.async_write_ha_state()  # Ensure last_notified is saved to state
