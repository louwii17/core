"""HomeKit irrigation system accessories."""

from datetime import datetime
import logging
from typing import Any, override

from pyhap.characteristic import Characteristic
from pyhap.const import CATEGORY_SPRINKLER
from pyhap.util import callback as pyhap_callback

from homeassistant.components.input_number import (
    ATTR_VALUE,
    CONF_MAX,
    CONF_MIN,
    CONF_STEP,
    SERVICE_SET_VALUE,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_UNIT_OF_MEASUREMENT,
    SERVICE_CLOSE_VALVE,
    SERVICE_OPEN_VALVE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTime,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HassJobType,
    HomeAssistant,
    State,
    callback,
    split_entity_id,
)
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import DurationConverter

from .accessories import HomeAccessory, HomeDriver
from .const import (
    CHAR_ACTIVE,
    CHAR_IN_USE,
    CHAR_NAME,
    CHAR_PROGRAM_MODE,
    CHAR_REMAINING_DURATION,
    CHAR_SERVICE_LABEL_INDEX,
    CHAR_SET_DURATION,
    CHAR_VALVE_TYPE,
    CONF_LINKED_PROGRAM_MODE_SENSOR,
    CONF_LINKED_VALVE_DURATION,
    CONF_LINKED_VALVE_END_TIME,
    CONF_ZONES,
    PROP_MAX_VALUE,
    PROP_MIN_STEP,
    PROP_MIN_VALUE,
    SERV_IRRIGATION_SYSTEM,
    SERV_VALVE,
)
from .type_switches import (
    VALVE_DURATION_MAX_DEFAULT,
    VALVE_DURATION_MIN_DEFAULT,
    VALVE_DURATION_STEP_DEFAULT,
    VALVE_OPEN_STATES,
    VALVE_REMAINING_TIME_MAX_DEFAULT,
)
from .util import cleanup_name_for_homekit

_LOGGER = logging.getLogger(__name__)

PROGRAM_MODE_NONE = 0
PROGRAM_MODE_SCHEDULED = 1
PROGRAM_MODE_MANUAL = 2


class IrrigationZone:
    """Represent one Valve service inside an irrigation accessory."""

    def __init__(
        self,
        accessory: IrrigationSystem,
        entity_id: str,
        name: str,
        index: int,
        config: dict[str, Any],
    ) -> None:
        """Initialize a zone service."""
        self.accessory = accessory
        self.hass = accessory.hass
        self.entity_id = entity_id
        self.linked_duration_entity: str | None = config.get(CONF_LINKED_VALVE_DURATION)
        self.linked_end_time_entity: str | None = config.get(CONF_LINKED_VALVE_END_TIME)

        optional_chars = [CHAR_NAME, CHAR_SERVICE_LABEL_INDEX]
        if self.linked_duration_entity:
            optional_chars.append(CHAR_SET_DURATION)
        if self.linked_end_time_entity:
            optional_chars.append(CHAR_REMAINING_DURATION)

        self.service = accessory.add_preload_service(
            SERV_VALVE, optional_chars, unique_id=entity_id
        )
        self.char_active = self.service.configure_char(
            CHAR_ACTIVE, value=0, setter_callback=self.set_state
        )
        self.char_in_use = self.service.configure_char(CHAR_IN_USE, value=0)
        self.service.configure_char(CHAR_VALVE_TYPE, value=1)
        self.service.configure_char(CHAR_NAME, value=cleanup_name_for_homekit(name))
        self.service.configure_char(CHAR_SERVICE_LABEL_INDEX, value=index)

        self.char_set_duration: Characteristic | None = None
        self.char_remaining_duration: Characteristic | None = None
        if self.linked_duration_entity:
            self.char_set_duration = self.service.configure_char(
                CHAR_SET_DURATION,
                value=self.get_duration(),
                setter_callback=self.set_duration,
                properties={
                    PROP_MIN_VALUE: self._duration_property(
                        CONF_MIN, VALVE_DURATION_MIN_DEFAULT
                    ),
                    PROP_MAX_VALUE: self._duration_property(
                        CONF_MAX, VALVE_DURATION_MAX_DEFAULT
                    ),
                    PROP_MIN_STEP: self._duration_property(
                        CONF_STEP, VALVE_DURATION_STEP_DEFAULT
                    ),
                },
            )
        if self.linked_end_time_entity:
            self.char_remaining_duration = self.service.configure_char(
                CHAR_REMAINING_DURATION,
                getter_callback=self.get_remaining_duration,
                properties={
                    PROP_MAX_VALUE: self._duration_property(
                        CONF_MAX, VALVE_REMAINING_TIME_MAX_DEFAULT
                    )
                },
            )

        self.update(self.hass.states.get(entity_id))

    @callback
    def set_state(self, value: bool) -> None:
        """Open or close this zone."""
        self.char_in_use.set_value(int(value))
        self.accessory.update_system_state()
        self.accessory.async_call_service(
            "valve",
            SERVICE_OPEN_VALVE if value else SERVICE_CLOSE_VALVE,
            {ATTR_ENTITY_ID: self.entity_id},
            value,
        )

    @callback
    def set_duration(self, value: int) -> None:
        """Set this zone's duration from HomeKit seconds."""
        assert self.linked_duration_entity
        state = self.hass.states.get(self.linked_duration_entity)
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) if state else None
        native_value = self._convert_duration(value, UnitOfTime.SECONDS, unit)
        self.accessory.async_call_service(
            split_entity_id(self.linked_duration_entity)[0],
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: self.linked_duration_entity, ATTR_VALUE: native_value},
            value,
        )

    @callback
    def update(self, state: State | None) -> None:
        """Update the zone characteristics."""
        is_open = state is not None and state.state in VALVE_OPEN_STATES
        self.char_active.set_value(int(is_open))
        self.char_in_use.set_value(int(is_open))
        if self.char_set_duration:
            self.char_set_duration.set_value(self.get_duration())
        if self.char_remaining_duration:
            self.char_remaining_duration.set_value(self.get_remaining_duration())

    def get_duration(self) -> int:
        """Return configured duration in HomeKit seconds."""
        if self.linked_duration_entity is None:
            return 0
        state = self.hass.states.get(self.linked_duration_entity)
        if state is None:
            return 0
        try:
            value = float(state.state)
        except ValueError:
            return 0
        return max(
            int(
                self._convert_duration(
                    value,
                    state.attributes.get(ATTR_UNIT_OF_MEASUREMENT),
                    UnitOfTime.SECONDS,
                )
            ),
            0,
        )

    def get_remaining_duration(self) -> int:
        """Return remaining duration in seconds."""
        if self.linked_end_time_entity is None:
            return self.get_duration() if self.char_in_use.value else 0
        state = self.hass.states.get(self.linked_end_time_entity)
        end_time: datetime | None = (
            dt_util.parse_datetime(state.state) if state is not None else None
        )
        if end_time is None:
            return self.get_duration() if self.char_in_use.value else 0
        return max(int((end_time - dt_util.utcnow()).total_seconds()), 0)

    def _duration_property(self, attribute: str, default: int) -> int:
        """Convert a linked duration entity property to seconds."""
        if self.linked_duration_entity is None:
            return default
        state = self.hass.states.get(self.linked_duration_entity)
        if state is None:
            return default
        value = state.attributes.get(attribute)
        if value is None:
            return default
        return int(
            self._convert_duration(
                value,
                state.attributes.get(ATTR_UNIT_OF_MEASUREMENT),
                UnitOfTime.SECONDS,
            )
        )

    @staticmethod
    def _convert_duration(
        value: float, from_unit: str | None, to_unit: str | None
    ) -> float:
        """Convert a duration, treating unknown units as seconds."""
        if from_unit not in DurationConverter.VALID_UNITS:
            from_unit = UnitOfTime.SECONDS
        if to_unit not in DurationConverter.VALID_UNITS:
            to_unit = UnitOfTime.SECONDS
        return DurationConverter.convert(value, from_unit, to_unit)


class IrrigationSystem(HomeAccessory):
    """Represent a HomeKit IrrigationSystem with linked Valve services."""

    def __init__(
        self,
        hass: HomeAssistant,
        driver: HomeDriver,
        name: str,
        system_id: str,
        aid: int,
        config: dict[str, Any],
        entity_config: dict[str, dict[str, Any]],
    ) -> None:
        """Initialize an irrigation system."""
        super().__init__(
            hass,
            driver,
            name,
            system_id,
            aid,
            config,
            category=CATEGORY_SPRINKLER,
            device_id=system_id,
        )
        self._available = True
        self.zone_entity_ids: list[str] = config[CONF_ZONES]
        self.program_mode_entity: str | None = config.get(
            CONF_LINKED_PROGRAM_MODE_SENSOR
        )

        self.service = self.add_preload_service(
            SERV_IRRIGATION_SYSTEM, [CHAR_REMAINING_DURATION]
        )
        self.set_primary_service(self.service)
        self.char_active = self.service.configure_char(
            CHAR_ACTIVE, value=0, setter_callback=self.set_active
        )
        self.char_in_use = self.service.configure_char(CHAR_IN_USE, value=0)
        self.char_program_mode = self.service.configure_char(
            CHAR_PROGRAM_MODE, value=PROGRAM_MODE_NONE
        )
        self.char_remaining_duration = self.service.configure_char(
            CHAR_REMAINING_DURATION,
            getter_callback=self.get_remaining_duration,
            properties={PROP_MAX_VALUE: VALVE_REMAINING_TIME_MAX_DEFAULT},
        )

        self.zones: dict[str, IrrigationZone] = {}
        for index, entity_id in enumerate(self.zone_entity_ids, start=1):
            state = hass.states.get(entity_id)
            zone_name = entity_config.get(entity_id, {}).get("name") or (
                state.name if state else entity_id
            )
            zone = IrrigationZone(
                self,
                entity_id,
                zone_name,
                index,
                entity_config.get(entity_id, {}),
            )
            self.zones[entity_id] = zone
            self.service.add_linked_service(zone.service)

        self._zone_subscriptions: list[CALLBACK_TYPE] = []
        self.update_system_state()

    @callback
    def set_active(self, value: bool) -> None:
        """Stop all zones when the irrigation system is deactivated."""
        if value:
            self.update_system_state()
            return
        for zone in self.zones.values():
            if zone.char_in_use.value:
                zone.set_state(False)

    @callback
    def get_remaining_duration(self) -> int:
        """Return the active zone's remaining duration."""
        return max(
            (zone.get_remaining_duration() for zone in self.zones.values()),
            default=0,
        )

    @callback
    def update_system_state(self) -> None:
        """Update controller-level characteristics."""
        in_use = any(zone.char_in_use.value for zone in self.zones.values())
        self.char_active.set_value(int(in_use))
        self.char_in_use.set_value(int(in_use))
        self.char_remaining_duration.set_value(self.get_remaining_duration())

        program_mode = PROGRAM_MODE_MANUAL if in_use else PROGRAM_MODE_NONE
        if self.program_mode_entity and (
            state := self.hass.states.get(self.program_mode_entity)
        ):
            program_state = state.state.casefold()
            if program_state == "scheduled":
                program_mode = PROGRAM_MODE_SCHEDULED
            elif program_state == "manual":
                program_mode = PROGRAM_MODE_MANUAL
        self.char_program_mode.set_value(program_mode)

    @callback
    def _async_state_changed(self, event: Event[EventStateChangedData]) -> None:
        """Handle a zone or linked entity state change."""
        entity_id = event.data["entity_id"]
        if zone := self.zones.get(entity_id):
            zone.update(event.data["new_state"])
        else:
            for zone in self.zones.values():
                if entity_id in (
                    zone.linked_duration_entity,
                    zone.linked_end_time_entity,
                ):
                    zone.update(self.hass.states.get(zone.entity_id))
        self.update_system_state()

    @pyhap_callback  # type: ignore[untyped-decorator]
    @callback
    @override
    def run(self) -> None:
        """Subscribe to every entity represented by this accessory."""
        tracked = set(self.zone_entity_ids)
        if self.program_mode_entity:
            tracked.add(self.program_mode_entity)
        for zone in self.zones.values():
            if zone.linked_duration_entity:
                tracked.add(zone.linked_duration_entity)
            if zone.linked_end_time_entity:
                tracked.add(zone.linked_end_time_entity)
        self._zone_subscriptions.append(
            async_track_state_change_event(
                self.hass,
                tracked,
                self._async_state_changed,
                job_type=HassJobType.Callback,
            )
        )

    @callback
    @override
    def async_stop(self) -> None:
        """Cancel irrigation system state subscriptions."""
        while self._zone_subscriptions:
            self._zone_subscriptions.pop()()
        super().async_stop()

    @property
    @override
    def available(self) -> bool:
        """Return whether at least one configured zone is available."""
        return any(
            (state := self.hass.states.get(entity_id)) is not None
            and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
            for entity_id in self.zone_entity_ids
        )

    @callback
    @override
    def async_update_state(self, new_state: State) -> None:
        """Satisfy the HomeAccessory interface; composite updates are routed above."""
