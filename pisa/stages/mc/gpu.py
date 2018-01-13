import time

import numpy as np
import pycuda.driver as cuda
import pycuda.autoinit

from pisa import ureg, Q_, FTYPE
from pisa.core.binning import OneDimBinning, MultiDimBinning
from pisa.core.events import Events
from pisa.core.map import Map, MapSet
from pisa.core.param import ParamSet
from pisa.core.stage import Stage
from pisa.stages.mc.GPUWeight import GPUWeight
from pisa.stages.osc.prob3gpu import prob3gpu
from pisa.utils.comparisons import normQuant
from pisa.utils.format import split
from pisa.utils.hash import hash_obj
from pisa.utils.log import logging
from pisa.utils.resources import find_resource
from copy import deepcopy


__all__ = ['gpu']

__author__ = 'P. Eller'

__license__ = '''Copyright (c) 2014-2017, The IceCube Collaboration

 Licensed under the Apache License, Version 2.0 (the "License");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an "AS IS" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.'''


class gpu(Stage):
    """
    GPU accelerated (pyCUDA) service to compute histograms based on reweighted
    MC events.

    Parameters
    ----------
    params : ParamSet or sequence with which to instantiate a ParamSet.
        Expected params are:

            detector_depth : float
            earth_model : PREM file path
            prop_height : quantity (dimensionless)
            YeI : quantity (dimensionless)
            YeO : quantity (dimensionless)
            YeM : quantity (dimensionless)
            theta12 : quantity (angle)
            theta13 : quantity (angle)
            theta23 : quantity (angle)
            deltam21 : quantity (mass^2)
            deltam31 : quantity (mass^2)
            deltacp : quantity (angle)
            no_nc_osc : bool
                don't oscillate NC events, but rather assign probabilities 1 for nue->nue and numu->numu, and 0 for nutau->nutau
            nu_nubar_ratio : quantity (dimensionless)
            nue_numu_ratio : quantity (dimensionless)
            livetime : quantity (time)
            aeff_scale : quantity (dimensionless)
            delta_index : quantity (dimensionless)
            Barr_uphor_ratio : quantity (dimensionless)
            Barr_nu_nubar_ratio : quantity (dimensionless)
            Genie_Ma_QE : quantity (dimensionless)
            Genie_Ma_RES : quantity (dimensionless)
            events_file : hdf5 file path (output from make_events), including flux weights and Genie systematics coefficients
            nutau_cc_norm : quantity (dimensionless)
            nutau_norm : quantity (dimensionless)
            reco_e_res_raw : quantity (dimensionless)
            reco_e_scale_raw : quantity (dimensionless)
            reco_cz_res_raw :quantity (dimensionless)
            bdt_cut : quantity (dimensionless)
            kde : bool
                apply KDE smoothing to outputs (d2d)
            hist_e_scale : quantity (dimensionless)
                scale factor for energy bin edges, as a reco E systematic
            true_e_scale : quantity (dimensionless)
                scale factor for true energy

    Notes
    -----
    This stage takes as inputs hdf5 events files that already iclude the
    nominal flux weights and genie systematic coefficients. The files are
    expected to contain
        true_energy : true energy of the event in GeV
        true_coszen : true coszen of the event
        reco_energy : reco energy of the event in GeV
        reco_coszen : reco coszen of the event
        neutrino_nue_flux : flux weight for nu_e in case of neutrino, or anti-nu_e in case of anti-neutrino event
        neutrino_numu_flux : flux weight for nu_mu in case of neutrino, or anti-nu_mu in case of anti-neutrino event
        neutrino_oppo_nue_flux:flux weight for anti-nu_e in case of neutrino, or nu_e in case of anti-neutrino event
        neutrino_oppo_numu_flux :flux weight for anti-nu_mu in case of neutrino, or nu_mu in case of anti-neutrino event
        weighted_aeff : effective are weight for event
        pid : pid value

    and more optional:
        dunkman_l5 : BDT value
        linear_fit_maccqe : Genie CC quasi elastic linear coefficient
        quad_fit_maccqe : Genie CC quasi elastic quadratic coefficient
        linear_fit_maccres : Genie CC resonance linear coefficient
        quad_fit_maccres : Genie CC resonance quadratic coefficient

    the dictionary self.events_dict is the central object here:
    it contains two dictionaries for every flavour:
        * host : all event arrays on CPU
        * device : pointers to the same arrays, but on GPU
    and some additional keys like:
        * n_evts : number of events
        * hist : retrieved histogram
        * ...

    All floats (arrays) on GPU are of type FTYPE, currently double precision

    """
    def __init__(self, params, output_binning, disk_cache=None,
                 memcache_deepcopy=True, error_method=None, output_names=None,
                 outputs_cache_depth=20, debug_mode=None):

        self.osc_params = (
            'detector_depth',
            'earth_model',
            'prop_height',
            'YeI',
            'YeO',
            'YeM',
            'theta12',
            'theta13',
            'theta23',
            'deltam21',
            'deltam31',
            'deltacp',
            'no_nc_osc',
        )

        self.true_params = (
            'true_e_scale',
        )

        self.flux_params = (
            'nu_nubar_ratio',
            'nue_numu_ratio',
            'delta_index',
            'Barr_uphor_ratio',
            'Barr_nu_nubar_ratio',
        )

        self.other_params = (
            'livetime',
            'aeff_scale',
            'Genie_Ma_QE',
            'Genie_Ma_RES',
            'events_file',
            'nutau_cc_norm',
            'nutau_norm',
            'reco_e_res_raw',
            'reco_e_scale_raw',
            'reco_cz_res_raw',
            'hist_e_scale',
            'hist_pid_scale',
            'bdt_cut',
            'kde',
            'cut_outer',
        )

        expected_params = (self.osc_params + self.flux_params +
                           self.other_params + self.true_params)


        super(self.__class__, self).__init__(
            use_transforms=False,
            params=params,
            expected_params=expected_params,
            output_names=output_names,
            error_method=error_method,
            memcache_deepcopy=memcache_deepcopy,
            outputs_cache_depth=outputs_cache_depth,
            output_binning=output_binning,
            debug_mode=debug_mode
        )

        if self.params.kde.value:
            from pisa.utils.kde_hist import kde_histogramdd
            self.kde_histogramdd = kde_histogramdd
        else:
            from pisa.utils.gpu_hist import GPUHist
            self.GPUHist = GPUHist

    def validate_params(self, params):
        # Not a good idea to scale nutau norm, without the NC events being
        # oscillated
        if params.nutau_norm.value != 1.0 or not params.nutau_norm.is_fixed:
            assert (params.no_nc_osc.value == False), 'If you want NC tau events scaled, you should oscillate them -> set no_nc_osc to False!!!'
        if params.hist_e_scale.is_fixed == False or params.hist_e_scale.value != 1.0:
            assert (params.kde.value == False), 'The hist_e_scale can only be used with histograms, not KDEs!'
        if params.hist_pid_scale.is_fixed == False or params.hist_pid_scale.value != 1.0:
            assert (params.kde.value == False), 'The hist_epid_scale can only be used with histograms, not KDEs!'

    def _compute_nominal_outputs(self):
        # Store hashes for caching that is done inside the stage
        self.osc_hash = None
        self.flux_hash = None

        # Reset fixed errors
        self.fixed_error = None

        # Get param subset wanted for oscillations class
        osc_params_subset = []
        for param in self.params:
            if self.params.earth_model.value is not None:
                if (param.name in self.osc_params) or \
                   (param.name in self.true_params):
                    osc_params_subset.append(param)
            else:
                if (param.name in self.osc_params) and \
                   (param.name != 'no_nc_osc'):
                    osc_params_subset.append(param)
                if param.name == 'nutau_norm':
                    osc_params_subset.append(param)

        osc_params_subset = ParamSet(osc_params_subset)

        if self.params.earth_model.value is not None:
            earth_model = find_resource(self.params.earth_model.value)
            YeI = self.params.YeI.value.m_as('dimensionless')
            YeO = self.params.YeO.value.m_as('dimensionless')
            YeM = self.params.YeM.value.m_as('dimensionless')
        else:
            earth_model = None
            from pisa.stages.osc.prob3cpu import prob3cpu
        prop_height = self.params.prop_height.value.m_as('km')
        detector_depth = self.params.detector_depth.value.m_as('km')

        # Initialize classes
        if earth_model is not None:
            self.osc = prob3gpu(
                params=osc_params_subset,
                input_binning=None,
                output_binning=None,
                error_method=None,
                memcache_deepcopy=False,
                transforms_cache_depth=0,
                outputs_cache_depth=0,
            )
        else:
            self.osc = prob3cpu(
                params=osc_params_subset,
                input_binning=None,
                output_binning=None,
                error_method=None,
                memcache_deepcopy=False,
                transforms_cache_depth=0,
                outputs_cache_depth=0,
            )

        # Weight calculator
        self.gpu_weight = GPUWeight()

        self.bin_names = self.output_binning.names
        self.bin_edges = []

        for i,name in enumerate(self.bin_names):
            if 'energy' in name:
                bin_edges = self.output_binning[name].bin_edges.to('GeV').magnitude.astype(FTYPE)
                self.e_bin_number = i
            else:
                bin_edges = self.output_binning[name].bin_edges.magnitude.astype(FTYPE)
            if 'pid' in name:
                self.pid_bin_number = i
            self.bin_edges.append(bin_edges)

        if self.params.kde.value:
            assert self.error_method == None

        else:
            # GPU histogramer
            bin_edges = deepcopy(self.bin_edges)
            bin_edges[self.e_bin_number] *= FTYPE(self.params.hist_e_scale.value.m_as('dimensionless'))
            if 'pid' in self.bin_names:
                bin_edges[self.pid_bin_number][1] *= FTYPE(self.params.hist_pid_scale.value.m_as('dimensionless'))
            self.histogrammer = self.GPUHist(*bin_edges)

        # load events
        self.load_events()

    def load_events(self):
        # --- Load events
        # open Events file
        evts = Events(self.params.events_file.value)
        if self.params.bdt_cut.value == None:
            bdt_cut = None
        else:
            bdt_cut = self.params.bdt_cut.value.m_as('dimensionless')

        # Load and copy events
        variables = [
            'true_energy',
            'true_coszen',
            'reco_energy',
            'reco_coszen',
            'neutrino_nue_flux',
            'neutrino_numu_flux',
            'neutrino_oppo_nue_flux',
            'neutrino_oppo_numu_flux',
            'weighted_aeff',
            'pid',
            'dunkman_L5',
            'linear_fit_MaCCQE',
            'quad_fit_MaCCQE',
            'linear_fit_MaCCRES',
            'quad_fit_MaCCRES',
        ]

        # Allocate empty arrays (filled with 1s) on GPU
        empty = [
            'prob_e',
            'prob_mu',
            'weight',
            'scaled_nue_flux',
            'scaled_numu_flux',
            'scaled_nue_flux_shape',
            'scaled_numu_flux_shape'
        ]
        if self.error_method in ['sumw2', 'fixed_sumw2']:
            empty += ['sumw2']

        # List of flavints to use and corresponding number used in several
        # parts of the code
        self.flavs = [
            'nue_cc',
            'numu_cc',
            'nutau_cc',
            'nue_nc',
            'numu_nc',
            'nutau_nc',
            'nuebar_cc',
            'numubar_cc',
            'nutaubar_cc',
            'nuebar_nc',
            'numubar_nc',
            'nutaubar_nc'
        ]

        # Corresponding numbers for the flavours defined above, needed bi Prob3
        kFlavs = [0, 1, 2] * 4
        kNuBars = [1] *6 + [-1] * 6

        for flav, kFlav, kNuBar in zip(self.flavs, kFlavs, kNuBars):
            cuts = []
            if self.params.cut_outer.value:
                for name, edge in zip(self.bin_names, self.bin_edges):
                    cuts.append(evts[flav][name] >= edge[0])
                    cuts.append(evts[flav][name] <= edge[-1])
            if evts[flav].has_key('dunkman_L5'):
                if bdt_cut is not None:
                    # only keep events using bdt_score > bdt_cut
                    l5_bdt_score = evts[flav]['dunkman_L5'].astype(FTYPE)
                    cuts.append(l5_bdt_score >= bdt_cut)
            if len(cuts) > 0:
                cut = np.all(cuts, axis=0)
                for var in variables:
                    try:
                        #if cut is not None:
                        evts[flav][var] = evts[flav][var][cut]
                    except KeyError:
                        pass

        logging.debug('read in events and copy to GPU')
        start_t = time.time()
        # setup all arrays that need to be put on GPU
        self.events_dict = {}
        for flav, kFlav, kNuBar in zip(self.flavs, kFlavs, kNuBars):
            self.events_dict[flav] = {}
            # neutrinos: 1, anti-neutrinos: -1
            self.events_dict[flav]['kNuBar'] = kNuBar
            # electron: 0, muon: 1, tau: 2
            self.events_dict[flav]['kFlav'] = kFlav
            # Host arrays
            self.events_dict[flav]['host'] = {}
            for var in variables:
                try:
                    self.events_dict[flav]['host'][var] = (
                        evts[flav][var].astype(FTYPE)
                    )
                except KeyError:
                    # If variable doesn't exist (e.g. axial mass coeffs, just
                    # fill in ones) only warn first time
                    if flav == self.flavs[0]:
                        logging.warning('replacing variable %s by ones'%var)
                    self.events_dict[flav]['host'][var] = np.ones_like(
                        evts[flav]['true_energy'],
                        dtype=FTYPE
                    )
            self.events_dict[flav]['n_evts'] = np.uint32(
                len(self.events_dict[flav]['host'][variables[0]])
            )
            #select even 50%
            #self.events_dict[flav]['host']['weighted_aeff'][::2] = 0
            #select odd 50%
            #self.events_dict[flav]['host']['weighted_aeff'][1::2] = 0
            #every 10th event only
            #cut = np.zeros_like(self.events_dict[flav]['host']['weighted_aeff'])
            #cut[9::10] = 1
            #self.events_dict[flav]['host']['weighted_aeff']*=cut
            for var in empty:
                if (self.params.no_nc_osc and
                        ((flav in ['nue_nc', 'nuebar_nc'] and var == 'prob_e')
                         or (flav in ['numu_nc', 'numubar_nc']
                             and var == 'prob_mu'))):
                    # In case of not oscillating NC events, we can set the
                    # probabilities of nue->nue and numu->numu at 1, and
                    # nutau->nutau at 0
                    self.events_dict[flav]['host'][var] = np.ones(
                        self.events_dict[flav]['n_evts'], dtype=FTYPE
                    )
                else:
                    self.events_dict[flav]['host'][var] = np.zeros(
                        self.events_dict[flav]['n_evts'], dtype=FTYPE
                    )
            # Calulate layers (every particle crosses a number of layers in the
            # earth with different densities, and for a given length these
            # depend only on the earth model (PREM) and the true coszen of an
            # event. Therefore we can calculate these for once and are done
            if self.params['earth_model'].value is not None:
                nlayers, dens, dist = self.osc.calc_layers(
                    self.events_dict[flav]['host']['true_coszen']
                )
                self.events_dict[flav]['host']['numLayers'] = nlayers
                self.events_dict[flav]['host']['densityInLayer'] = dens
                self.events_dict[flav]['host']['distanceInLayer'] = dist

        end_t = time.time()
        logging.debug('layers done in %.4f ms'%((end_t - start_t) * 1000))

        # Copy arrays to GPU
        start_t = time.time()
        for flav in self.flavs:
            self.events_dict[flav]['device'] = {}
            for key, val in self.events_dict[flav]['host'].items():
                self.events_dict[flav]['device'][key] = cuda.mem_alloc(
                    val.nbytes
                )
                cuda.memcpy_htod(self.events_dict[flav]['device'][key], val)
        end_t = time.time()
        logging.debug('copy done in %.4f ms'%((end_t - start_t) * 1000))

        # Apply raw reco sys
        self.apply_reco()

    def apply_reco(self):
        """Apply raw reco systematics (to use as inputs to polyfit stage)"""
        for flav in self.flavs:
            # Apply energy reco sys
            f = self.params.reco_e_res_raw.value.m_as('dimensionless')
            g = self.params.reco_e_scale_raw.value.m_as('dimensionless')
            self.events_dict[flav]['host']['reco_energy'] = (
                g * ((1.-f) * self.events_dict[flav]['host']['true_energy']
                     + f * self.events_dict[flav]['host']['reco_energy'])
            ).astype(FTYPE)

            # Apply coszen reco sys
            f = self.params.reco_cz_res_raw.value.m_as('dimensionless')
            self.events_dict[flav]['host']['reco_coszen'] = (
                (1.-f) * self.events_dict[flav]['host']['true_coszen']
                + f * self.events_dict[flav]['host']['reco_coszen']
            ).astype(FTYPE)

            # Make sure everything is within -1 <= coszen <= 1, otherwise
            # reflect
            reco_cz = self.events_dict[flav]['host']['reco_coszen']
            lt_m1_mask = reco_cz < -1
            gt_p1_mask = reco_cz > 1
            while np.any(lt_m1_mask + gt_p1_mask):
                reco_cz[gt_p1_mask] = 2 - reco_cz[gt_p1_mask]
                reco_cz[lt_m1_mask] = -2 - reco_cz[lt_m1_mask]
                lt_m1_mask = reco_cz < -1
                gt_p1_mask = reco_cz > 1

            self.update_device_arrays(flav, 'reco_energy')
            self.update_device_arrays(flav, 'reco_coszen')

    def update_device_arrays(self, flav, var):
        """Helper function to update device arrays"""
        self.events_dict[flav]['device'][var].free()
        self.events_dict[flav]['device'][var] = cuda.mem_alloc(
            self.events_dict[flav]['host'][var].nbytes
        )
        cuda.memcpy_htod(
            self.events_dict[flav]['device'][var],
            self.events_dict[flav]['host'][var]
        )

    def get_device_arrays(self, variables=['weight']):
        """Copy back event by event information into the host dict"""
        for flav in self.flavs:
            for var in variables:
                buff = np.full(self.events_dict[flav]['n_evts'],
                               fill_value=np.nan, dtype=FTYPE)
                cuda.memcpy_dtoh(buff, self.events_dict[flav]['device'][var])
                assert np.all(np.isfinite(buff))
                self.events_dict[flav]['host'][var] = buff

    def sum_array(self, x, n_evts):
        """Helper function to compute the sum over a device array"""
        out = np.array([0.], dtype=FTYPE)
        d_out = cuda.mem_alloc(out.nbytes)
        cuda.memcpy_htod(d_out, out)
        self.gpu_weight.calc_sum(n_evts, x, d_out)
        cuda.memcpy_dtoh(out, d_out)
        return out[0]

    def _compute_outputs(self, inputs=None):
        logging.debug('retreive weighted histo')

        # Get hash to decide whether expensive stuff needs to be recalculated
        osc_param_vals = [self.params[name].value for name in self.osc_params]
        gpu_flux_vals = [self.params[name].value
                           for name in self.flux_params]
        
        true_params_vals = [self.params[name].value for name in self.true_params]

        osc_param_vals += true_params_vals
        gpu_flux_vals += true_params_vals

        if self.full_hash:
            osc_param_vals = normQuant(osc_param_vals)
            gpu_flux_vals = normQuant(gpu_flux_vals)
        osc_hash = hash_obj(osc_param_vals, full_hash=self.full_hash)
        flux_hash = hash_obj(gpu_flux_vals, full_hash=self.full_hash)

        recalc_osc = not (osc_hash == self.osc_hash)
        recalc_flux = not (flux_hash == self.flux_hash)

        livetime = self.params.livetime.value.m_as('seconds')
        aeff_scale = self.params.aeff_scale.value.m_as('dimensionless')
        Genie_Ma_QE = self.params.Genie_Ma_QE.value.m_as('dimensionless')
        Genie_Ma_RES = self.params.Genie_Ma_RES.value.m_as('dimensionless')
        true_e_scale = self.params.true_e_scale.value.m_as('dimensionless')

        if recalc_flux:
            nue_numu_ratio = self.params.nue_numu_ratio.value.m_as('dimensionless')
            nu_nubar_ratio = self.params.nu_nubar_ratio.value.m_as('dimensionless')
            delta_index = self.params.delta_index.value.m_as('dimensionless')
            Barr_uphor_ratio = self.params.Barr_uphor_ratio.value.m_as('dimensionless')
            Barr_nu_nubar_ratio = self.params.Barr_nu_nubar_ratio.value.m_as('dimensionless')

        if recalc_osc:
            if self.params['earth_model'].value is not None:
                theta12 = self.params.theta12.value.m_as('rad')
                theta13 = self.params.theta13.value.m_as('rad')
                theta23 = self.params.theta23.value.m_as('rad')
                deltam21 = self.params.deltam21.value.m_as('eV**2')
                deltam31 = self.params.deltam31.value.m_as('eV**2')
                deltacp = self.params.deltacp.value.m_as('rad')
                self.osc.update_MNS(theta12, theta13, theta23,
                                    deltam21, deltam31, deltacp)

        tot = 0
        start_t = time.time()
        for flav in self.flavs:
            # Calculate osc probs, filling the device arrays with probabilities
            if recalc_osc:
                if not (self.params.no_nc_osc.value and flav.endswith('_nc')):
                    if self.params['earth_model'].value is not None:
                        self.osc.calc_probs(
                            self.events_dict[flav]['kNuBar'],
                            self.events_dict[flav]['kFlav'],
                            self.events_dict[flav]['n_evts'],
                            true_e_scale=true_e_scale,
                            **self.events_dict[flav]['device']
                        )
                    else:
                        # Vacuum is done on a CPU
                        self.osc.calc_probs(
                            self.events_dict[flav]['kNuBar'],
                            self.events_dict[flav]['kFlav'],
                            self.events_dict[flav]['n_evts'],
                            true_e_scale=true_e_scale,
                            **self.events_dict[flav]['host']
                        )
                        # Then need to update the device arrays
                        self.update_device_arrays(flav, 'prob_e')
                        self.update_device_arrays(flav, 'prob_mu')

            # Calculate weights
            if recalc_flux:
                # Calcukate the flux weights
                self.gpu_weight.calc_flux(
                    self.events_dict[flav]['n_evts'],
                    nue_numu_ratio=nue_numu_ratio,
                    nu_nubar_ratio=nu_nubar_ratio,
                    kNuBar=self.events_dict[flav]['kNuBar'],
                    delta_index=delta_index,
                    Barr_uphor_ratio=Barr_uphor_ratio,
                    Barr_nu_nubar_ratio=Barr_nu_nubar_ratio,
                    true_e_scale=true_e_scale,
                    **self.events_dict[flav]['device']
                )

            # Calculate global scales for flux normalizations
            #nue_flux_norm_n = self.sum_array(self.events_dict[flav]['device']['scaled_nue_flux'], self.events_dict[flav]['n_evts'])
            #nue_flux_norm_d = self.sum_array(self.events_dict[flav]['device']['scaled_nue_flux_shape'], self.events_dict[flav]['n_evts'])
            #nue_flux_norm = nue_flux_norm_n / nue_flux_norm_d
            nue_flux_norm = 1.
            #numu_flux_norm_n = self.sum_array(self.events_dict[flav]['device']['scaled_numu_flux'], self.events_dict[flav]['n_evts'])
            #numu_flux_norm_d = self.sum_array(self.events_dict[flav]['device']['scaled_numu_flux_shape'], self.events_dict[flav]['n_evts'])
            #numu_flux_norm = numu_flux_norm_n / numu_flux_norm_d
            numu_flux_norm = 1.

            # Calculate the event weights, from osc. probs and flux weights
            # global scaling factors for the nue and numu flux can be
            # given, for normalization purposes
            self.gpu_weight.calc_weight(
                self.events_dict[flav]['n_evts'],
                livetime=livetime,
                nue_flux_norm=nue_flux_norm,
                numu_flux_norm=numu_flux_norm,
                aeff_scale=aeff_scale,
                kNuBar=self.events_dict[flav]['kNuBar'],
                Genie_Ma_QE=Genie_Ma_QE,
                Genie_Ma_RES=Genie_Ma_RES,
                true_e_scale=true_e_scale,
                **self.events_dict[flav]['device']
            )

            # Calculate weights squared, for error propagation
            if self.error_method in ['sumw2', 'fixed_sumw2']:
                self.gpu_weight.calc_sumw2(
                    self.events_dict[flav]['n_evts'],
                    **self.events_dict[flav]['device']
                )

            tot += self.events_dict[flav]['n_evts']

        end_t = time.time()
        logging.debug('GPU calc done in %.4f ms for %s events'
                      %(((end_t - start_t) * 1000), tot))

        if self.params.kde.value:
            start_t = time.time()
            #copy back weights
            self.get_device_arrays(variables=['weight'])

            for flav in self.flavs:
                # loop over pid bins and for every bin evaluate the KDEs
                # and put them together into a 3d array
                data = np.array([
                    self.events_dict[flav]['host'][self.bin_names[0]],
                    self.events_dict[flav]['host'][self.bin_names[1]],
                    self.events_dict[flav]['host'][self.bin_names[2]]
                ])
                weights = self.events_dict[flav]['host']['weight']
                hist = self.kde_histogramdd(
                        data.T,
                        weights=weights,
                        binning=self.output_binning,
                        coszen_name='reco_coszen',
                        use_cuda=True,
                        bw_method='silverman',
                        alpha=0.8,
                        oversample=1,
                        coszen_reflection=0.5,
                        adaptive=True
                    )
                self.events_dict[flav]['hist'] = hist
            end_t = time.time()
            logging.debug('KDE done in %.4f ms for %s events'
                          %(((end_t - start_t) * 1000), tot))
        else:
            # hist_e_scale:
            bin_edges = deepcopy(self.bin_edges)
            bin_edges[self.e_bin_number] *= FTYPE(self.params.hist_e_scale.value.m_as('dimensionless'))
            if 'pid' in self.bin_names:
                bin_edges[self.pid_bin_number][1] *= FTYPE(self.params.hist_pid_scale.value.m_as('dimensionless'))
            self.histogrammer.update_bin_edges(*bin_edges)

            start_t = time.time()
            # Histogram events and download fromm GPU, if either weights or
            # osc changed
            if len(self.bin_names) == 2:
                for flav in self.flavs:
                    hist = self.histogrammer.get_hist(
                        self.events_dict[flav]['n_evts'],
                        d_x = self.events_dict[flav]['device'][self.bin_names[0]],
                        d_y = self.events_dict[flav]['device'][self.bin_names[1]],
                        d_w = self.events_dict[flav]['device']['weight']
                    )
                    self.events_dict[flav]['hist'] = hist

                    if self.error_method in ['sumw2', 'fixed_sumw2']:
                        sumw2 = self.histogrammer.get_hist(
                            self.events_dict[flav]['n_evts'],
                            d_x=self.events_dict[flav]['device'][self.bin_names[0]],
                            d_y=self.events_dict[flav]['device'][self.bin_names[1]],
                            d_w=self.events_dict[flav]['device']['sumw2']
                        )
                        self.events_dict[flav]['sumw2'] = sumw2
            else:
                for flav in self.flavs:
                    hist = self.histogrammer.get_hist(
                        self.events_dict[flav]['n_evts'],
                        d_x=self.events_dict[flav]['device'][self.bin_names[0]],
                        d_y=self.events_dict[flav]['device'][self.bin_names[1]],
                        d_z=self.events_dict[flav]['device'][self.bin_names[2]],
                        d_w=self.events_dict[flav]['device']['weight']
                    )
                    self.events_dict[flav]['hist'] = hist

                    if self.error_method in ['sumw2', 'fixed_sumw2']:
                        sumw2 = self.histogrammer.get_hist(
                            self.events_dict[flav]['n_evts'],
                            d_x=self.events_dict[flav]['device'][self.bin_names[0]],
                            d_y=self.events_dict[flav]['device'][self.bin_names[1]],
                            d_z=self.events_dict[flav]['device'][self.bin_names[2]],
                            d_w=self.events_dict[flav]['device']['sumw2']
                        )
                        self.events_dict[flav]['sumw2'] = sumw2

            end_t = time.time()
            logging.debug('GPU hist done in %.4f ms for %s events'
                          %(((end_t - start_t) * 1000), tot))

        # Set new hash
        self.osc_hash = osc_hash
        self.flux_hash = flux_hash

        # Add histos together into output names, and apply nutau normalizations
        # errors (sumw2) are also added, while scales are applied in quadrature
        # of course
        out_hists = {}
        out_sumw2 = {}
        for name in self.output_names:
            for flav in self.flavs:
                f = 1.0
                if flav in ['nutau_cc', 'nutaubar_cc']:
                    f *= self.params.nutau_cc_norm.value.m_as('dimensionless')
                if 'nutau' in flav:
                    f *= self.params.nutau_norm.value.m_as('dimensionless')
                if ('bar_nc' in flav and 'allbar_nc' in name) or ('_nc' in flav and 'all_nc' in name) or (flav in name):
                    if out_hists.has_key(name):
                        out_hists[name] += self.events_dict[flav]['hist'] * f
                        if self.error_method in ['sumw2', 'fixed_sumw2']:
                            out_sumw2[name] += self.events_dict[flav]['sumw2'] * f * f
                    else:
                        out_hists[name] = np.copy(self.events_dict[flav]['hist']) * f
                        if self.error_method in ['sumw2', 'fixed_sumw2']:
                            out_sumw2[name] = np.copy(self.events_dict[flav]['sumw2']) * f * f

        # Pack everything in a final PISA MapSet
        maps = []
        for name, hist in out_hists.items():
            if self.error_method == 'sumw2':
                maps.append(Map(name=name, hist=hist,
                                error_hist=np.sqrt(out_sumw2[name]),
                                binning=self.output_binning))
            # This is a special case where we always want the error to be the
            # same....so for the first Mapet it is taken from the calculation,
            # and every following time it is just euqal to the first one
            elif self.error_method == 'fixed_sumw2':
                if self.fixed_error == None:
                    self.fixed_error = {}
                if not self.fixed_error.has_key(name):
                    self.fixed_error[name] = np.sqrt(out_sumw2[name])
                maps.append(Map(name=name, hist=hist,
                                error_hist=self.fixed_error[name],
                                binning=self.output_binning))
            else:
                maps.append(Map(name=name, hist=hist,
                                binning=self.output_binning))

        return MapSet(maps, name='gpu_mc')
