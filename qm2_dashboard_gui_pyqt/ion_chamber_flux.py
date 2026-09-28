"""
ion_chamber_flux.py — X-ray flux from ion chamber counts.

A Python port of the CHESS "Ion Chamber Flux Calculator"
(https://www.chess.cornell.edu/userstechnical-resourcescalculators/ion-chamber-flux-calculator),
© Peter Revesz (pr20@cornell.edu), CHESS, (2007). last change: 11/12/2013.
X-ray attenuation/absorption data are from the NIST FFAST database
(https://physics.nist.gov/PhysRefData/FFast/html/form.html); the average
ionization energies are from the SLAC web site
(http://www-ssrl.slac.stanford.edu/mes/xafs/flux.html).

The model: the ion chamber current comes from single ionization of the gas;
secondary effects such as charge recombination or space charge are not
included. The counts are the rate of a voltage-to-frequency converter
(1 MHz per 10 V) reading the current amplifier, so

    current = counts * 10 / 1e6 * gain            [A]
    flux(E) = current * W / e / E / (1 - exp(-L * mu(E) * rho))

at each tabulated energy, interpolated quadratically to the requested
energy, then divided by the transmission of one 1-mil Kapton entrance
window. W is the gas's average ionization energy, mu the gas's coefficient
for the chosen absorption mechanism, rho its density and L the chamber
length.

Ported as is, including the original data tables. Two things differ from or
are worth knowing about the original:
- For Xe, the "Mass photoabsorption coefficient" and "Total mass
  attenuation coefficient" tables are copies of He's in the original, so
  those two Xe choices give He-like results.
- The original interpolates through the last tabulated energy below the
  requested one and the next two, so above 80 keV it needs a point past
  100 keV, and at or below 4 keV one before 4 keV; both give NaN on the web
  page. Here the start of the three points is clamped to the table, so
  80-100 keV uses the 60, 80 and 100 keV points. Like the original,
  energies outside 5-100 keV are only warned about.
"""

import math
from typing import Dict, List

# Tabulated energies (keV) for all coefficient tables below.
ENERGIES_KEV: List[float] = [4.0, 5.0, 6.0, 8.0, 10.0, 15.0, 20.0, 30.0,
                             40.0, 50.0, 60.0, 80.0, 100.0]

METHODS: List[str] = [
    "Total less elastic",
    "Mass-energy absorption coefficient",
    "Mass photoabsorption coefficient",
    "Total mass attenuation coefficient",
]

# Per gas: default average ionization energy (eV) and density (g/cm3, sea
# level at 25 C), the coefficient table (cm2/g) for each absorption
# mechanism in METHODS order, and the total attenuation table ("aGas").
GASES: Dict[str, Dict] = {
    "Nitrogen": {
        "e_ion": 34.6,
        "density": 1.131e-03,
        "methods": [
            [6.105e+01, 3.097e+01, 1.770e+01, 7.290e+00, 3.676e+00, 1.116e+00, 5.375e-01,
             2.643e-01, 2.029e-01, 1.806e-01, 1.693e-01, 1.566e-01, 1.482e-01],
            [6.094e+01, 3.086e+01, 1.759e+01, 7.170e+00, 3.545e+00, 9.715e-01, 3.867e-01,
             1.099e-01, 5.051e-02, 3.217e-02, 2.548e-02, 2.211e-02, 2.231e-02],
            [6.097e+01, 3.087e+01, 1.759e+01, 7.167e+00, 3.543e+00, 9.674e-01, 3.808e-01,
             1.010e-01, 3.907e-02, 1.866e-02, 1.019e-02, 3.919e-03, 1.869e-03],
            [6.166e+01, 3.144e+01, 1.808e+01, 7.559e+00, 3.879e+00, 1.236e+00, 6.179e-01,
             3.066e-01, 2.288e-01, 1.980e-01, 1.817e-01, 1.639e-01, 1.529e-01],
        ],
        "total": [6.166e+01, 3.144e+01, 1.808e+01, 7.559e+00, 3.879e+00, 1.236e+00, 6.179e-01,
                  3.066e-01, 2.288e-01, 1.980e-01, 1.817e-01, 1.639e-01, 1.529e-01],
    },
    "Argon": {
        "e_ion": 26.2,
        "density": 1.613e-03,
        "methods": [
            [7.555e+02, 4.211e+02, 2.582e+02, 1.171e+02, 6.241e+01, 1.938e+01, 8.328e+00,
             2.535e+00, 1.126e+00, 6.305e-01, 4.146e-01, 2.449e-01, 1.837e-01],
            [6.979e+02, 3.953e+02, 2.449e+02, 1.125e+02, 6.038e+01, 1.886e+01, 8.074e+00,
             2.382e+00, 9.907e-01, 5.020e-01, 2.904e-01, 1.280e-01, 7.344e-02],
            [7.554e+02, 4.210e+02, 2.581e+02, 1.170e+02, 6.232e+01, 1.927e+01, 8.207e+00,
             2.403e+00, 9.909e-01, 4.946e-01, 2.793e-01, 1.127e-01, 5.564e-02],
            [7.572e+02, 4.225e+02, 2.593e+02, 1.180e+02, 6.316e+01, 1.983e+01, 8.629e+00,
             2.697e+00, 1.228e+00, 7.012e-01, 4.664e-01, 2.760e-01, 2.043e-01],
        ],
        "total": [7.572e+02, 4.225e+02, 2.593e+02, 1.180e+02, 6.316e+01, 1.983e+01, 8.629e+00,
                  2.697e+00, 1.228e+00, 7.012e-01, 4.664e-01, 2.760e-01, 2.043e-01],
    },
    "He": {
        "e_ion": 41.5,
        "density": 1.613e-04,
        "methods": [
            [7.265e-01, 4.151e-01, 2.915e-01, 2.091e-01, 1.885e-01, 1.796e-01, 1.785e-01,
             1.757e-01, 1.716e-01, 1.673e-01, 1.630e-01, 1.550e-01, 1.478e-01],
            [6.379e-01, 3.061e-01, 1.671e-01, 6.446e-02, 3.260e-02, 1.246e-02, 9.410e-03,
             1.003e-02, 1.190e-02, 1.375e-02, 1.544e-02, 1.826e-02, 2.047e-02],
            [6.370e-01, 3.048e-01, 1.654e-01, 6.184e-02, 2.920e-02, 7.335e-03, 2.752e-03,
             6.873e-04, 2.565e-04, 1.195e-04, 6.408e-05, 2.401e-05, 1.126e-05],
            [9.329e-01, 5.766e-01, 4.195e-01, 2.933e-01, 2.476e-01, 2.092e-01, 1.960e-01,
             1.838e-01, 1.763e-01, 1.703e-01, 1.651e-01, 1.562e-01, 1.486e-01],
        ],
        "total": [9.329e-01, 5.766e-01, 4.195e-01, 2.933e-01, 2.476e-01, 2.092e-01, 1.960e-01,
                  1.838e-01, 1.763e-01, 1.703e-01, 1.651e-01, 1.562e-01, 1.486e-01],
    },
    "Xe": {
        "e_ion": 22.0,
        "density": 5.323e-03,
        "methods": [
            [3.73e+02, 6.34e+02, 6.33e+02, 3.00e+02, 1.66e+02, 5.56e+01, 2.52e+01,
             8.17e+00, 2.22e+01, 1.24e+01, 7.56e+00, 3.47e+00, 1.90e+00],
            [3.728e+02, 6.015e+02, 5.979e+02, 2.871e+02, 1.605e+02, 5.420e+01, 2.465e+01,
             7.969e+00, 9.323e+00, 6.540e+00, 4.541e+00, 2.374e+00, 1.376e+00],
            # As in the original: a copy of He's table (see module docstring).
            [6.370e-01, 3.048e-01, 1.654e-01, 6.184e-02, 2.920e-02, 7.335e-03, 2.752e-03,
             6.873e-04, 2.565e-04, 1.195e-04, 6.408e-05, 2.401e-05, 1.126e-05],
            # As in the original: a copy of He's table (see module docstring).
            [9.329e-01, 5.766e-01, 4.195e-01, 2.933e-01, 2.476e-01, 2.092e-01, 1.960e-01,
             1.838e-01, 1.763e-01, 1.703e-01, 1.651e-01, 1.562e-01, 1.486e-01],
        ],
        "total": [3.79e+02, 6.39e+02, 6.37e+02, 3.03e+02, 1.69e+02, 5.74e+01, 2.65e+01,
                  8.93e+00, 2.27e+01, 1.27e+01, 7.82e+00, 3.63e+00, 2.01e+00],
    },
    "Kr": {
        "e_ion": 24.0,
        "density": 3.388e-03,
        "methods": [
            [6.15e+02, 3.39e+02, 2.07e+02, 9.41e+01, 5.07e+01, 1.16e+02, 5.47e+01,
             1.81e+01, 8.11e+00, 4.33e+00, 2.59e+00, 1.18e+00, 6.63e-01],
            [6.101e+02, 3.371e+02, 2.060e+02, 9.371e+01, 5.044e+01, 6.112e+01, 3.509e+01,
             1.365e+01, 6.538e+00, 3.596e+00, 2.178e+00, 9.729e-01, 5.192e-01],
            [6.15e+02, 3.39e+02, 2.07e+02, 9.41e+01, 5.06e+01, 1.16e+02, 5.46e+01,
             1.80e+01, 7.99e+00, 4.21e+00, 2.47e+00, 1.06e+00, 5.46e-01],
            [6.19e+02, 3.43e+02, 2.10e+02, 9.65e+01, 5.26e+01, 1.17e+02, 5.55e+01,
             1.85e+01, 8.39e+00, 4.52e+00, 2.74e+00, 1.27e+00, 7.22e-01],
        ],
        "total": [6.19e+02, 3.43e+02, 2.10e+02, 9.65e+01, 5.26e+01, 1.17e+02, 5.55e+01,
                  1.85e+01, 8.39e+00, 4.52e+00, 2.74e+00, 1.27e+00, 7.22e-01],
    },
}

ELECTRON_CHARGE = 1.602e-19   # C, as in the original
KAPTON_THICKNESS_CM = 2.54 / 10000.0   # one 1-mil Kapton window
STANDARD_LENGTHS_CM = {"short": 6.0, "long": 27.0}   # standard CHESS chambers
VALID_RANGE_EV = (5000.0, 100000.0)


def _quadratic(values: List[float], energy_kev: float) -> float:
    """The original's 3-point quadratic interpolation of a table at
    ENERGIES_KEV, through the last tabulated energy below `energy_kev` and
    the next two (the start index is clamped so it stays in the table)."""
    e = ENERGIES_KEV
    i = next((k for k, ek in enumerate(e) if ek >= energy_kev), len(e)) - 1
    i = min(max(i, 0), len(e) - 3)
    c2 = ((values[i] - values[i + 1]) / (e[i] - e[i + 1])
          - (values[i] - values[i + 2]) / (e[i] - e[i + 2]))
    c2 /= e[i + 1] - e[i + 2]
    c1 = (values[i] - values[i + 1]) / (e[i] - e[i + 1]) - c2 * (e[i] + e[i + 1])
    c0 = values[i] - c1 * e[i] - c2 * e[i] * e[i]
    return c0 + c1 * energy_kev + c2 * energy_kev * energy_kev


def kapton_attenuation(energy_ev: float) -> float:
    """Kapton linear attenuation (1/cm) from the original's fit; taken as 0
    at 30 keV and above."""
    if energy_ev >= 30000.0:
        return 0.0
    return (0.29874
            + 211.98374 * math.exp(-energy_ev / 2110.30858)
            + 12.58911 * math.exp(-energy_ev / 5511.62307)
            + 2308.65512 * math.exp(-energy_ev / 937.54182))


def ion_chamber_flux(gas: str, method: int, e_ion: float, density: float,
                     energy_ev: float, length_cm: float, counts: float,
                     gain: float) -> Dict[str, float]:
    """Flux from ion chamber counts, as the CHESS calculator computes it.

    gas: a key of GASES; method: index into METHODS; e_ion: average
    ionization energy (eV); density: gas density (g/cm3); energy_ev: X-ray
    energy (eV); length_cm: chamber length; counts: counter rate (Hz);
    gain: current amplifier range (A/V).

    Returns {"current" (A), "flux" (ph/s), "a_gas" (cm2/g), "a_gas_cm"
    (1/cm), "transmission"}.
    """
    table = GASES[gas]
    energy_kev = energy_ev / 1000.0
    current = counts * 10 / 1e6 * gain

    flux_table = []
    for mu, e_kev in zip(table["methods"][method], ENERGIES_KEV):
        absorbed = 1 - math.exp(-length_cm * mu * density)
        flux_table.append(current * e_ion / ELECTRON_CHARGE / (e_kev * 1000) / absorbed)
    flux = _quadratic(flux_table, energy_kev)

    a_gas = _quadratic(table["total"], energy_kev)
    a_gas_cm = a_gas * density

    window = math.exp(-KAPTON_THICKNESS_CM * kapton_attenuation(energy_ev))
    return {
        "current": current,
        "flux": flux / window,
        "a_gas": a_gas,
        "a_gas_cm": a_gas_cm,
        "transmission": window * window * math.exp(-length_cm * a_gas_cm),
    }
