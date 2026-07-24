"""Tests for HomeKit irrigation system accessories."""

from freezegun.api import FrozenDateTimeFactory

from homeassistant.components.homekit.const import (
    CONF_LINKED_PROGRAM_MODE_SENSOR,
    CONF_LINKED_VALVE_DURATION,
    CONF_LINKED_VALVE_END_TIME,
    CONF_ZONES,
    SERV_IRRIGATION_SYSTEM,
    SERV_VALVE,
)
from homeassistant.components.homekit.type_irrigation_systems import IrrigationSystem
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_CLOSE_VALVE,
    SERVICE_OPEN_VALVE,
    STATE_CLOSED,
    STATE_OPEN,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant

from tests.common import async_mock_service


async def test_irrigation_system(
    hass: HomeAssistant, hk_driver, freezer: FrozenDateTimeFactory
) -> None:
    """Test a grouped irrigation system and its zone services."""
    freezer.move_to("2026-07-23 12:00:00+00:00")
    hass.states.async_set("valve.front", STATE_CLOSED)
    hass.states.async_set("valve.back", STATE_CLOSED)
    hass.states.async_set(
        "number.front_duration",
        "10",
        {
            "min": 1,
            "max": 60,
            "step": 1,
            "unit_of_measurement": UnitOfTime.MINUTES,
        },
    )
    hass.states.async_set("sensor.front_end", "unknown")
    hass.states.async_set("sensor.program_mode", "scheduled")

    accessory = IrrigationSystem(
        hass,
        hk_driver,
        "Yard",
        "irrigation_system.yard",
        2,
        {
            CONF_ZONES: ["valve.front", "valve.back"],
            CONF_LINKED_PROGRAM_MODE_SENSOR: "sensor.program_mode",
        },
        {
            "valve.front": {
                CONF_LINKED_VALVE_DURATION: "number.front_duration",
                CONF_LINKED_VALVE_END_TIME: "sensor.front_end",
            }
        },
    )
    accessory.run()

    assert accessory.category == 28
    assert accessory.get_service(SERV_IRRIGATION_SYSTEM) is accessory.service
    assert len(accessory.services) == 4  # Accessory information, system, two zones
    assert (
        sum(service.display_name == SERV_VALVE for service in accessory.services) == 2
    )
    assert accessory.service.linked_services == [
        accessory.zones["valve.front"].service,
        accessory.zones["valve.back"].service,
    ]
    assert accessory.char_program_mode.value == 1
    assert accessory.zones["valve.front"].char_set_duration.value == 600

    open_calls = async_mock_service(hass, "valve", SERVICE_OPEN_VALVE)
    close_calls = async_mock_service(hass, "valve", SERVICE_CLOSE_VALVE)
    accessory.zones["valve.front"].char_active.client_update_value(1)
    await hass.async_block_till_done()
    assert open_calls[0].data[ATTR_ENTITY_ID] == "valve.front"

    hass.states.async_set("valve.front", STATE_OPEN)
    hass.states.async_set("sensor.program_mode", "manual")
    hass.states.async_set("sensor.front_end", "2026-07-23T12:05:00+00:00")
    await hass.async_block_till_done()
    assert accessory.char_active.value == 1
    assert accessory.char_in_use.value == 1
    assert accessory.char_program_mode.value == 2
    assert accessory.char_remaining_duration.value == 300

    accessory.char_active.client_update_value(0)
    await hass.async_block_till_done()
    assert close_calls[0].data[ATTR_ENTITY_ID] == "valve.front"
