"""Custom exceptions for PolEnergia API client."""


class PolEnergiaError(Exception):
    """Base exception for PolEnergia API errors."""


class PolEnergiaAuthorizationError(PolEnergiaError):
    """Exception raised when authentication fails."""


class PolEnergiaConnectionError(PolEnergiaError):
    """Exception raised when connection to API fails."""


class PolEnergiaAPIError(PolEnergiaError):
    """Exception raised when API returns an error."""


class PolEnergiaNoDataError(PolEnergiaError):
    """Exception raised when no data is available."""


class PolEnergiaInvalidResponseError(PolEnergiaError):
    """Exception raised when API response is invalid or unexpected."""
