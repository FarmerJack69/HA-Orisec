"""Config flow: choose the link (network / USB / serial bridge), reconfigure, re-authenticate."""

from __future__ import annotations

import os
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig, SelectSelectorMode

from .client import TRANSPORT_SERIAL, TRANSPORT_TCP, TRANSPORT_UDP, OrisecAuthError, OrisecClient, OrisecError
from .const import (
    BAUD_RATES,
    CONF_BAUD,
    CONF_DEVICE,
    CONF_POLL_INTERVAL,
    CONF_CLOCK_SYNC,
    CONF_CLOCK_TOLERANCE,
    DEFAULT_CLOCK_SYNC,
    DEFAULT_CLOCK_TOLERANCE,
    CONF_TRANSPORT,
    DEFAULT_BAUD,
    DEFAULT_POLL_SECONDS,
    DOMAIN,
)
from .protocol import DEFAULT_PORT

_BY_ID = "/dev/serial/by-id"
# Labels are given here as well as in translations/en.json: Home Assistant shows
# these directly, so the choices still read correctly when the browser holds an
# older copy of the translations (for example after replacing another `orisec`
# integration), where a plain list showed blank buttons.
_MENU = {
    "usb": "USB cable (panel micro-USB into this Home Assistant)",
    "network": "Network (CM2 comms module)",
    "bridge": "Serial bridge (ESP32 / ser2net over TCP)",
}


def _serial_ports() -> list[str]:
    """Stable /dev/serial/by-id paths, STM32 (Orisec panel) first."""
    try:
        ports = [os.path.join(_BY_ID, n) for n in sorted(os.listdir(_BY_ID))]
    except OSError:
        ports = []
    ports.sort(key=lambda s: 0 if "STM32" in s else 1)
    return ports


def _code_field(default: str = "") -> dict:
    return {vol.Required(CONF_PASSWORD, default=default): str}


async def _try_login(data: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (serial, error_key)."""
    if not str(data.get(CONF_PASSWORD, "")).isdigit():
        return None, "invalid_code"
    client = OrisecClient(
        data[CONF_PASSWORD],
        transport=data.get(CONF_TRANSPORT, TRANSPORT_UDP),
        host=data.get(CONF_HOST),
        port=data.get(CONF_PORT),
        device=data.get(CONF_DEVICE),
        baud=int(data.get(CONF_BAUD, DEFAULT_BAUD)),
    )
    try:
        info = await client.login()
    except OrisecAuthError:
        return None, "invalid_auth"
    except (OrisecError, OSError):
        return None, "cannot_connect"
    finally:
        await client.close()
    return info.serial or client.label, None


class _LinkSteps:
    """Shared form steps for the three link types (used by setup and reconfigure)."""

    _defaults: dict[str, Any]
    _ports: list[str]

    def _network_schema(self) -> vol.Schema:
        d = self._defaults
        return vol.Schema({
            vol.Required(CONF_HOST, default=d.get(CONF_HOST, "")): str,
            vol.Required(CONF_PORT, default=d.get(CONF_PORT, DEFAULT_PORT)): vol.All(int, vol.Range(1, 65535)),
            **_code_field(d.get(CONF_PASSWORD, "")),
        })

    def _usb_schema(self) -> vol.Schema:
        d = self._defaults
        ports = self._ports
        default = d.get(CONF_DEVICE) or (ports[0] if ports else "/dev/ttyACM0")
        return vol.Schema({
            vol.Required(CONF_DEVICE, default=default): SelectSelector(
                SelectSelectorConfig(options=ports or [default], custom_value=True, mode=SelectSelectorMode.DROPDOWN)
            ),
            vol.Required(CONF_BAUD, default=str(d.get(CONF_BAUD, DEFAULT_BAUD))): SelectSelector(
                SelectSelectorConfig(
                    options=[str(b) for b in BAUD_RATES],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            **_code_field(d.get(CONF_PASSWORD, "")),
        })

    def _bridge_schema(self) -> vol.Schema:
        d = self._defaults
        return vol.Schema({
            vol.Required(CONF_HOST, default=d.get(CONF_HOST, "")): str,
            vol.Required(CONF_PORT, default=d.get(CONF_PORT, 6638)): vol.All(int, vol.Range(1, 65535)),
            **_code_field(d.get(CONF_PASSWORD, "")),
        })


class OrisecConfigFlow(_LinkSteps, ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._defaults: dict[str, Any] = {}
        self._reconfiguring = False
        self._ports: list[str] = []

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return OrisecOptionsFlow()

    # --- entry points ---------------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=_MENU)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._reconfiguring = True
        self._defaults = dict(self._get_reconfigure_entry().data)
        return self.async_show_menu(step_id="reconfigure", menu_options=_MENU)

    # --- link steps -----------------------------------------------------------

    async def async_step_network(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._link_step("network", TRANSPORT_UDP, self._network_schema, user_input)

    async def async_step_usb(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._ports = await self.hass.async_add_executor_job(_serial_ports)
        return await self._link_step("usb", TRANSPORT_SERIAL, self._usb_schema, user_input)

    async def async_step_bridge(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._link_step("bridge", TRANSPORT_TCP, self._bridge_schema, user_input)

    async def _link_step(self, step_id, transport, schema, user_input) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {CONF_TRANSPORT: transport, **user_input}
            if transport == TRANSPORT_SERIAL:
                data.pop(CONF_HOST, None)
                data.pop(CONF_PORT, None)
                if CONF_BAUD in data:
                    data[CONF_BAUD] = int(data[CONF_BAUD])
            else:
                data.pop(CONF_DEVICE, None)
                data.pop(CONF_BAUD, None)
            serial, err = await _try_login(data)
            if err:
                errors["base"] = err
                self._defaults = {**self._defaults, **user_input}
            else:
                await self.async_set_unique_id(serial)
                if self._reconfiguring:
                    self._abort_if_unique_id_mismatch(reason="wrong_panel")
                    entry = self._get_reconfigure_entry()
                    return self.async_update_reload_and_abort(entry, data=data)
                self._abort_if_unique_id_configured(updates=data)
                return self.async_create_entry(title=f"Orisec {serial}", data=data)
        return self.async_show_form(step_id=step_id, data_schema=schema(), errors=errors)

    # --- re-authentication ----------------------------------------------------

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            _, err = await _try_login({**entry.data, **user_input})
            if err:
                errors["base"] = err
            else:
                return self.async_update_reload_and_abort(entry, data_updates=user_input)
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=vol.Schema(_code_field()), errors=errors
        )


class OrisecOptionsFlow(OptionsFlow):
    """Settings → Devices & services → Orisec → Configure."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        opts = self.config_entry.options
        current = opts.get(CONF_POLL_INTERVAL, DEFAULT_POLL_SECONDS)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(CONF_POLL_INTERVAL, default=current): vol.All(vol.Coerce(float), vol.Range(min=1, max=60)),
                vol.Required(CONF_CLOCK_SYNC, default=opts.get(CONF_CLOCK_SYNC, DEFAULT_CLOCK_SYNC)): bool,
                vol.Required(
                    CONF_CLOCK_TOLERANCE, default=opts.get(CONF_CLOCK_TOLERANCE, DEFAULT_CLOCK_TOLERANCE)
                ): vol.All(vol.Coerce(int), vol.Range(min=3, max=600)),
            }),
        )
