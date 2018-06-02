"""TODO:"""
import math
from types import SimpleNamespace

import numpy as np

import fluxpart.util as util
from .containers import FVSPResult, MassFluxes, RootSoln, WQCData


def fvspart_progressive(w, q, c, wue, adjust_fluxes=True):
    """FVS flux partitioning using high frequency eddy covariance data.

    If a valid partitioning solution is not found for the passed series
    data, low-frequency (large-scale) components are progressively
    removed from the data until either a valid solution is found or
    the series decomposition is exhausted.

    Parameters
    ----------
    w,q,c : array_like
        1D high frequency time series for vertical wind speed `w` (m/s),
        water vapor `q` (kg/m^3), and CO2 `c` (kg/m^3).
    wue : float, `wue` < 0
        leaf-level water use efficiency, kg CO2 / kg H2O.
    adjust_fluxes : bool, optional
        Indicates whether the obtained partitioned fluxes should be
        adjusted so that the totals match the original data. Default is
        True.

    Returns
    -------
    :class:`~fluxpart.containers.FVSPResult`

    Notes
    -----
    If a valid partitioning is not found, the returned `numersoln` and
    `wqc_data` correspond to the final iteration attempted.

    """
    max_decomp_lvl = int(np.log2(w.size))
    wq_tot = np.cov((w, q))[0, 1]
    wc_tot = np.cov((w, c))[0, 1]

    # The loop progressively filters the data until a physically valid
    # partitioning is found or the loop/filter is exhausted. The first
    # iteration of progressive_lowcut removes only the mean value
    # (q'=q-<q>, etc.), so the first iteration uses the "unfiltered"
    # deviations.

    for cnt, lowcut_wqc in enumerate(_progressive_lowcut(w, q, c)):
        wave_lvl = (max_decomp_lvl - cnt, max_decomp_lvl)
        fvsp = fvspart_series(*lowcut_wqc, wue)

        if fvsp.rootsoln.isvalid:
            if adjust_fluxes:
                fvsp.fluxes = _adjust_fluxes(fvsp.fluxes, wue, wq_tot, wc_tot)
                fvsp.valid_partition, fvsp.mssg = _isvalid_partition(
                    fvsp.fluxes
                )
            if fvsp.valid_partition:
                break

    fvsp.wave_lvl = wave_lvl
    if not fvsp.rootsoln.isvalid:
        fvsp.valid_partition = False
        fvsp.mssg = fvsp.rootsoln.mssg
    if not fvsp.valid_partition:
        fvsp.fluxes = MassFluxes()
    return fvsp


def fvspart_series(w, q, c, wue, wipe_if_invalid=False):
    """FVS partition q and c fluxes using high frequency eddy cov data.

    Parameters
    ----------
    w,q,c : array_like
        1D high frequency time series for vertical wind speed `w` (m/s),
        water vapor `q` (kg/m^3), and CO2 `c` (kg/m^3).
    wue : float, `wue` < 0
        leaf-level water use efficiency, kg CO2 / kg H2O.
    wipe_if_invalid : boolean
        If True, return default (nan) values for all mass fluxes if any
        calculated fluxes violate directional requirements. Default is
        False.

    Returns
    -------
    :class:`~fluxpart.containers.FVSPResult`,

    """
    cov = np.cov([w, q, c])
    wqc_data = WQCData(
        wq=cov[0, 1],
        wc=cov[0, 2],
        var_q=cov[1, 1],
        var_c=cov[2, 2],
        corr_qc=cov[1, 2] / math.sqrt(cov[1, 1] * cov[2, 2]),
    )
    return fvspart_interval(wqc_data, wue)


def fvspart_interval(wqc_data, wue, wipe_if_invalid=False):
    """Partition H2O and CO2 fluxes using interval averaged data values.

    Parameters
    ----------
    wqc_data : :class:`~fluxpart.containers.WQCData`
    wue : float, kg CO2 / kg H2O
        Leaf-level water use efficiency, `wue` < 0
    wipe_if_invalid : boolean
        If True, return default (nan) values for all mass fluxes if any
        calculated fluxes violate directional requirements. Default is
        False.

    Returns
    -------
    :class:`~fluxpart.containers.FVSPResult`

    """
    rootsoln = findroot(wqc_data, wue)
    if not rootsoln.isvalid:
        return FVSPResult(
            wqc_data=wqc_data,
            rootsoln=rootsoln,
            fluxes=MassFluxes(),
            valid_partition=False,
            mssg=rootsoln.mssg,
        )
    mass_fluxes = _mass_fluxes(
        var_cp=rootsoln.var_cp,
        corr_cp_cr=rootsoln.corr_cp_cr,
        wqc_data=wqc_data,
        wue=wue,
        co2soln_id=rootsoln.co2soln_id,
    )
    isvalid, mssg = _isvalid_partition(mass_fluxes)
    if not isvalid and wipe_if_invalid:
        mass_fluxes = MassFluxes()
    return FVSPResult(
        wqc_data=wqc_data,
        rootsoln=rootsoln,
        fluxes=mass_fluxes,
        valid_partition=isvalid,
        mssg=mssg,
    )


def findroot(wqc_data, wue):
    """Calculate (corr_cp_cr, var_cp).

    Parameters
    ----------
    wqc_data : namedtuple or equivalent namespace
        :class:`~fluxpart.containers.WQCData`
    wue : float
        Leaf-level water use efficiency, `wue` < 0, kg CO2 / kg H2O.

    Returns
    -------
    namedtuple
        :class:`~fluxpart.containers.RootSoln`

    """
    # scale dimensional parameters so they have comparable magnitudes
    # H20: kg - > g
    # CO2: kg - > mg

    var_q = wqc_data.var_q * 1e6
    var_c = wqc_data.var_c * 1e12
    wq = wqc_data.wq * 1e3
    wc = wqc_data.wc * 1e6
    corr_qc = wqc_data.corr_qc
    wue = wue * 1e3

    sd_q, sd_c = math.sqrt(var_q), math.sqrt(var_c)

    numer = -2 * corr_qc * sd_c * sd_q * wq * wc
    numer += var_c * wq ** 2 + var_q * wc ** 2
    numer *= -(corr_qc ** 2 - 1) * var_c * var_q * wue ** 2
    denom = -corr_qc * sd_c * sd_q * (wc + wq * wue)
    denom += var_c * wq + var_q * wc * wue
    denom = denom ** 2

    var_cp = numer / denom

    numer = -(corr_qc ** 2 - 1) * var_c * var_q * (wc - wq * wue) ** 2
    denom = -2 * corr_qc * sd_c * sd_q * wc * wq
    denom += var_c * wq ** 2 + var_q * wc ** 2
    denom *= -2 * corr_qc * sd_c * sd_q * wue + var_c + var_q * wue ** 2

    rho_sq = numer / denom
    corr_cp_cr = -math.sqrt(rho_sq)

    valid_root, valid_mssg = _isvalid_root(corr_cp_cr, var_cp)

    co2soln_id = None
    sig_cr = np.nan
    if valid_root:
        valid_root = False
        valid_mssg = "Trial root did not satisfy equations"
        scaled_wqc_data = WQCData(
            wq=wq, wc=wc, var_q=var_q, var_c=var_c, corr_qc=corr_qc
        )

        tol = 1e-12
        r0 = _residual_func((corr_cp_cr, var_cp), scaled_wqc_data, wue, 0)
        r1 = _residual_func((corr_cp_cr, var_cp), scaled_wqc_data, wue, 1)

        if abs(r0[0]) < tol and abs(r0[1]) < tol:
            co2soln_id = 0
            valid_root = True
            valid_mssg = ""
        if abs(r1[0]) < tol and abs(r1[1]) < tol:
            assert not co2soln_id
            co2soln_id = 1
            valid_root = True
            valid_mssg = ""

        if valid_root:
            wqc_data = SimpleNamespace(
                var_q=var_q, var_c=var_c, wq=wq, wc=wc, corr_qc=corr_qc
            )

            wcr_ov_wcp = flux_ratio(
                var_cp, corr_cp_cr, wqc_data, "co2", co2soln_id
            )
            sig_cr = wcr_ov_wcp * math.sqrt(var_cp) / corr_cp_cr

    # re-scale dimensional variables to SI units
    return RootSoln(
        corr_cp_cr=corr_cp_cr,
        var_cp=var_cp * 1e-12,
        sig_cr=sig_cr * 1e-6,
        co2soln_id=co2soln_id,
        isvalid=valid_root,
        mssg=valid_mssg,
    )


def flux_ratio(var_cp, corr_cp_cr, wqc_data, ftype, farg):
    """Compute the nonstomatal:stomatal ratio of the H2O or CO2 flux.

    The ratio (either wqe/wqt or wcr/wcp) is found by solving Eq. 13
    of [SS08]_.

    Parameters
    ---------
    wqc_data : namedtuple or equivalent namespace
        :class:`~fluxpart.containers.WQCData`
    ftype : {'co2', 'h2o'}
        Specifies whether the flux is CO2 or H2O
    farg : number
        If `ftype` = 'co2', then `farg` = {0 or 1} specifies the root
        of Eq. 13b to be used to calculate the CO2 flux ratio wcr/wcp:
        `farg`=1 uses the '+' solution, `farg`=0 uses the '-' solution.
        If `ftype` = 'h2o', then `farg` is a float equal to the water
        use efficiency (wue < 0), kg/kg.

    Returns
    -------
    fratio : float or np.nan
        The requested flux ratio, wqe/wqt or wcr/wcp. Set to np.nan
        if solution is not real.

    Notes
    -----
    When solving for wqe/wqt, the '-' solution of the quadratic Eq. 13a
    is not relevant because only the '+' solution yields wqe/wqt > 0,
    which is required/assumed by the physical model in [SS08]_.

    """
    if ftype == "co2" or ftype == "CO2":
        sign = 1 if farg == 1 else -1
        num = wqc_data.var_c
    elif ftype == "h2o" or ftype == "H2O":
        sign = 1
        num = farg ** 2 * wqc_data.var_q
    else:
        raise ValueError("ftype must be 'co2' or 'h2o'")
    disc = 1 - 1 / corr_cp_cr ** 2 + num / var_cp / corr_cp_cr ** 2
    if disc < 0:
        fratio = np.nan
    else:
        fratio = corr_cp_cr ** 2 * (sign * math.sqrt(disc) - 1)
    return fratio


def _mass_fluxes(var_cp, corr_cp_cr, wqc_data, wue, co2soln_id):
    """Calculate flux components for given (var_cp, corr_cp_cr) pair."""
    wcr_ov_wcp = flux_ratio(var_cp, corr_cp_cr, wqc_data, "co2", co2soln_id)
    # TODO: handle wcr_ov_wcp ~ -1
    wcp = wqc_data.wc / (wcr_ov_wcp + 1)
    wcr = wqc_data.wc - wcp
    wqt = wcp / wue
    wqe = wqc_data.wq - wqt
    return MassFluxes(
        Fq=wqc_data.wq, Fqt=wqt, Fqe=wqe, Fc=wqc_data.wc, Fcp=wcp, Fcr=wcr
    )


def _residual_func(x, wqc_data, wue, co2soln_id):
    """Residual function used with root finding routine.

    The two components of the residual are Eqs. 7a and 7b of [SAAS+18]_.

    """
    corr_cp_cr, var_cp = x
    wcr_ov_wcp = flux_ratio(var_cp, corr_cp_cr, wqc_data, "co2", co2soln_id)
    wqe_ov_wqt = flux_ratio(var_cp, corr_cp_cr, wqc_data, "h2o", wue)

    # Eq. 7a
    lhs = wue * wqc_data.wq / wqc_data.wc * (wcr_ov_wcp + 1)
    rhs = wqe_ov_wqt + 1
    resid1 = lhs - rhs

    # Eq. 7b
    lhs = wue * wqc_data.corr_qc
    lhs *= math.sqrt(wqc_data.var_c * wqc_data.var_q) / var_cp
    rhs = 1 + wqe_ov_wqt + wcr_ov_wcp
    rhs += wqe_ov_wqt * wcr_ov_wcp / corr_cp_cr ** 2
    resid2 = lhs - rhs
    return [resid1, resid2]


def _isvalid_root(corr_cp_cr, var_cp):
    isvalid = True
    mssg = ""
    if var_cp <= 0:
        isvalid = False
        mssg += "var_cp <= 0; "
    if not -1 < corr_cp_cr < 0:
        isvalid = False
        mssg += "corr_cp_cr <-1 OR >0; "
    # TODO: could add other bound checks?
    return isvalid, mssg


def _isvalid_partition(flux_components):
    """Test if partitioned flux directions (signs) are valid."""
    fc = flux_components
    isvalid = True
    mssg = ""
    if fc.Fqt <= 0:
        isvalid = False
        mssg += "Fqt <= 0; "
    if fc.Fqe <= 0:
        isvalid = False
        mssg += "Fqe <= 0; "
    if fc.Fcp >= 0:
        isvalid = False
        mssg += "Fcp >= 0; "
    if fc.Fcr <= 0:
        isvalid = False
        mssg += "Fcr <= 0; "
    return isvalid, mssg


def _adjust_fluxes(flux_components, wue, Fq_tot, Fc_tot):
    """Adjust partitioned fluxes so they match measured totals.

    If filtering has been applied to the series data, covariances in
    the filtered data may differ from those in the original data.
    Consequently, partitioned flux totals may not match exactly the
    total fluxes indicated by the original data. Here, partitioned
    fluxes are adjusted proportionally so that they match the totals in
    the original data.

    Parameters
    ----------
    flux_components : namedtuple or equivalent
        Attributes (floats) specify the mass flux components (Fq, Fqe,
        Fq, Fc, Fcr, Fcp), kg/m^2/s.
    wue : float
        Leaf-level water use efficiency (`wue` < 0), kg CO2 / kg H2O
    Fq_tot, Fc_tot : float
        Desired net total H2O (`Fq_tot`) and CO2 (`Fc_tot`) fluxes,
        kg/m^2/s.

    Returns
    -------
    namedtuple
        :class:`~fluxpart.containers.MassFluxes`

    """
    fc = flux_components
    Fq_diff = Fq_tot - (fc.Fqe + fc.Fqt)
    Fqe = fc.Fqe + Fq_diff * (fc.Fqe / (fc.Fqt + fc.Fqe))
    Fqt = Fq_tot - Fqe
    Fcp = wue * Fqt
    Fcr = Fc_tot - Fcp
    return MassFluxes(Fq=Fq_tot, Fqt=Fqt, Fqe=Fqe, Fc=Fc_tot, Fcp=Fcp, Fcr=Fcr)


def _progressive_lowcut(wind, vapor, co2):
    """Apply progressive lowcut filter to wind, vapor, and CO2 series.

    Use wavelet decomposition to yield a sequence of (w, q, c) series
    in which low frequency (large scale) components are progressively
    removed from w, q, c.

    Parameters
    ----------
    wind,vapor,co2 : array-like
        1D time series of vertical wind velocity (w), water vapor
        concentration (q), and carbon dioxide concentration (c).

    Yields
    ------
    (lc_w, lc_q, lc_c) : tuple of arrays
        Arrays lc_w, lc_q, and lc_c are low cut (high pass) filtered
        versions of the passed w,q,c series.

    Notes
    -----
    Before the filter is applied, the data are truncated so that the
    length is a power of 2.

    """
    max_pow2_len = 2 ** int(np.log2(np.asarray(co2).shape[0]))
    trunc_w = np.asarray(wind)[:max_pow2_len]
    trunc_q = np.asarray(vapor)[:max_pow2_len]
    trunc_c = np.asarray(co2)[:max_pow2_len]
    lowcut_w = util.progressive_lowcut_series(trunc_w)
    lowcut_q = util.progressive_lowcut_series(trunc_q)
    lowcut_c = util.progressive_lowcut_series(trunc_c)
    for lowcut_series in zip(lowcut_w, lowcut_q, lowcut_c):
        yield lowcut_series
