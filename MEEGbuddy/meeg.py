import mne
from mne.io import Raw, RawArray,set_eeg_reference,BaseRaw
from mne.preprocessing import (ICA, read_ica, create_eog_epochs,
                               create_ecg_epochs, fix_stim_artifact,
                               maxwell_filter)
from mne.epochs import concatenate_epochs
from pandas import read_csv, DataFrame
from mne import (compute_covariance, Epochs, EpochsArray, find_events,
                 pick_types,read_source_estimate, compute_morph_matrix,
                 set_log_level, read_trans, read_bem_solution,
                 make_forward_solution, read_epochs, read_source_spaces,
                 BaseEpochs, read_evokeds, EvokedArray, read_labels_from_annot,
                 Label)
from mne.utils import set_config
from mne.time_frequency import (tfr_morlet,tfr_array_morlet,
                                tfr_array_multitaper,AverageTFR,morlet)
from mne.minimum_norm import (make_inverse_operator,apply_inverse_epochs,
                              apply_inverse,read_inverse_operator,
                              write_inverse_operator,source_induced_power,
                              source_band_induced_power)
import glob,re
import numpy as np
import os
from .psd_multitaper_plot_tools import ButtonClickProcessor
from scipy.stats import stats, mstats
from autoreject import AutoReject, compute_thresholds, set_matplotlib_defaults
try:
    import seaborn as sns
    import matplotlib.pyplot as plt
    import matplotlib.patches as patches
    import matplotlib.ticker as ticker
    from matplotlib.colors import SymLogNorm,LogNorm
except:
    print('Unable to import plot tools.')
from functools import partial
from scipy import linalg
from mne.connectivity import spectral_connectivity
from mne.stats import permutation_cluster_test
from tqdm import tqdm
from joblib import Parallel,delayed
from . import pci
from .gif_combine import combine_gifs
import nitime.algorithms as tsa
from scipy import interpolate
from scipy.stats import linregress
from scipy.signal import detrend
from scipy.io import savemat
from surfer import Brain
try:
    import naturalneighbor
except:
    print('No naturalneighbor')
import warnings
from mne.chpi import read_head_pos
try:
   from mne.viz import plot_head_positions
except:
   print('Unable to import MNE visualization')
try:
    from mayavi import mlab
except:
    print('Unable to import mayavi')

class MEEGbuddy:
    '''
    Takes in one or more .fif files for a subject that are combined together
    into an MNE raw object and one behavior csv file assumed to be combined.
    The bad channels are then auto-marked,
    ICA is then auto-marked and then plots are presented for you to ok,
    epochs are made, and autoreject is applied.
    All data is saved automatically in BIDS-inspired format.
    '''
    def __init__(self, subject, fdata, behavior, baseline, stimuli, eeg=False,
                 meg=False, response=None, task=None, no_response=None,
                 exclude_response=None, tbuffer=1, subjects_dir=None,
                 epochs=None, event=None, seed=551832):
        '''
        fdata: one or more .fif files with event triggers and EEG data
        fbehavior: dictionary with variable names indexing a list of attributes
        baseline: a list of stim channel start and stop time for the baseline.
            exclude: indices of behavior to exclude due to no response ect.
        stimuli: a dictionary of the name of each stimulus to epoch
            with a list of the stim channel, the start and stop times as the values.
        response: a list of the stimulus channel, start and stop times
            that will correctly account for missed triggers from the behavior
        exclude_response: optional reponses to exclude for things like
            malfunctioning equiptment, experimenter error ect.
        '''
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        mne.set_log_level('error')
        set_log_level("ERROR")
        self.subjects_dir = subjects_dir
        if isinstance(fdata,list):
            if self.subjects_dir is None:
                self.subjects_dir = os.path.dirname(os.path.realpath(fdata[0]))
                for f in fdata:
                    subjects_dir_current = os.path.dirname(os.path.realpath(f))
                    if subjects_dir_current != subjects_dir:
                        warn('Directory estabilished as the directory for ' +
                             'the first fdata entry.')
        else:
            if self.subjects_dir is None:
                self.subjects_dir = os.path.dirname(os.path.realpath(fdata))
            fdata = [fdata]

        self.fdata = fdata
        self.meg = meg
        self.eeg = eeg

        self.subject = subject
        self.task = task

        processes = ['ica','epochs','TFR','noreun_phi','mat_files',
                     'CPT','plots','sources','raw_preprocessed','evoked',
                     'psd_multitaper','behavior']
        if subjects_dir is None:
            subjects_dir = os.getcwd() + '/'

        self.process_dirs = {}
        for process in processes:
            self.process_dirs[process] = (subjects_dir + '/' + process +
                                          '/' + self.subject + '/')
            if not os.path.exists(self.process_dirs[process]):
                os.makedirs(self.process_dirs[process])

        self.behavior = behavior
        if self.behavior:
            param_lengths = [len(self.behavior[param]) for param in self.behavior]
            if len(np.unique(param_lengths)) > 1:
                raise ValueError('All behavior parameters must index lists of the same length.')
            self.n = param_lengths[0]

        if no_response is None:
            self.no_response = []
        else:
            self.no_response = no_response

        if exclude_response is None:
            self.exclude_response = []
        else:
            self.exclude_response = [trial for trial in exclude_response if
                                     trial not in self.no_response]

        self.events = stimuli
        if any([len(self.events[event]) != 3 for event in self.events]):
            raise ValueError('There must be a channel, start and stop time for each stimulus.')

        if response:
            if len(response) != 3:
                raise ValueError('response must contain a channel, start time and stop time.')
            self.events['Response'] = response

        self.baseline = baseline
        if not self.baseline or len(self.baseline) != 3:
            print('Baseline must contain a channel, start time and stop time. ' +
                  'Okay to continue, use normalized=False when making epochs')
        self.events['Baseline'] = baseline

        self.tbuffer = tbuffer

        if epochs is not None and event is not None:
            self._save_epochs(epochs,event)

        self._load_behavior()

        np.random.seed(seed)

    def preprocess(self,event=None):
        # preprocessing
        self.autoMarkBads()
        self.findICA()
        for event in self.getEvents():
            self.makeEpochs(event)
            self.markAutoReject(event)

    def _fname(self,process_dir,keyword,ftype,*tags):
        # must give process dir, any tags
        fname = self.process_dirs[process_dir] + self.subject
        if self.task:
            fname += '_' + self.task
        if self.eeg:
            fname += '_eeg'
        if self.meg:
            fname += '_meg'
        for tag in tags:
            if tag:
                fname += '_' + str(tag)
        fname += '-' + keyword
        if ftype:
            fname += ftype
        return fname

    def _save_behavior(self,behavior=None):
        if behavior is None:
            behavior = self.behavior
        print('Saving behavior')
        np.savez_compressed(self._fname('behavior','behavior','.npz'),
                            behavior=behavior)

    def _load_behavior(self):
        fname = self._fname('behavior','behavior','.npz')
        if os.path.isfile(fname):
            print('Loading saved behavior')
            self.behavior = np.load(fname)['behavior'].item()

    def _save_raw_preprocessed(self,raw,ica=False,keyword=None):
        print('Saving raw ' + 'preprocessed'*(not ica and keyword is None) +
              'ica'*ica + '%s' %(keyword)*(keyword is not None))
        raw.save(self._fname('raw_preprocessed','raw','.fif','ica'*ica,keyword),
                 verbose=False,overwrite=True)

    def _load_raw(self,preprocessed=False,ica=False,keyword=None):
        if keyword or ica:
            preprocessed = False
        if keyword:
            ica = False
        if preprocessed or ica or keyword:
            if os.path.isfile(self._fname('raw_preprocessed','raw','.fif',
                                          'ica'*ica,keyword)):
                raw = Raw(self._fname('raw_preprocessed','raw','.fif','ica'*ica,
                                      keyword),verbose=False,preload=True)
                print('Preprocessed'*preprocessed + 'ICA'*ica +
                      '%s' %(keyword)*(keyword is not None)+' raw data loaded.')
            else:
                raise ValueError('No ' + 'preproccessed '*preprocessed +
                                 'ICA '*ica + 'raw data file found' +
                                 ' for %s' %(keyword)*(keyword is not None))
        else:
            f = self.fdata[0]
            print(f)
            raw = Raw(f, preload=True, verbose=False)
            raw.info['bads'] = []
            for f in self.fdata[1:]:
                print(f)
                r = Raw(f, preload=True, verbose=False)
                r.info['bads'] = []
                raw.append(r)
            if self.eeg:
                raw = raw.set_eeg_reference(ref_channels=[],projection=False)
            raw = raw.pick_types(meg=self.meg,eeg=self.eeg,stim=True,
                                 eog=True,ecg=True,emg=True)
        return raw

    def remove(self,event=None,preprocessed=False,ica=False,ar=False,
               keyword=None):
        dir_name = 'raw_preprocessed' if event is None else 'epochs'
        suffix = 'epo' if event else 'raw'
        fname = self._fname(dir_name,suffix,'.fif',event,keyword)
        if os.path.isfile(fname):
            os.remove(fname)

    def _save_ICA(self,ica,keyword=None):
        print('Saving ICA %s' %(keyword if keyword is not None else ''))
        ica.save(self._fname('ica','ica','.fif',keyword))

    def _load_ICA(self,keyword=None):
        fname = self._fname('ica','ica','.fif',keyword)
        if os.path.isfile(fname):
            ica = read_ica(fname)
            print('ICA loaded.')
            return ica
        else:
            print('No ICA data file found %s'
                    %(keyword if keyword is not None else ''))

    def _default_aux(self,inst,eogs,ecgs):
        if eogs is None:
            inds = pick_types(inst.info,meg=False,eog=True)
            eogs = [inst.ch_names[ind] for ind in inds]
            print('Using ' + ' '.join(eogs) + ' as eogs')
        if ecgs is None:
            inds = pick_types(inst.info,meg=False,ecg=True)
            ecgs = [inst.ch_names[ind] for ind in inds]
            print('Using ' + ' '.join(ecgs) + ' as ecgs')
        return eogs,ecgs

    def _combine_insts(self,insts):
        if len(insts) < 1:
            raise ValueError('Nothing to combine')
        inst_data = insts[0]._data
        inst_info = insts[0].info
        for inst in insts[1:]:
            inst_data = np.concatenate([inst_data,inst._data],axis=-2)
            inst_info['ch_names'] += inst.info['ch_names']
            inst_info['chs'] += inst.info['chs']
            inst_info['nchan'] += inst.info['nchan']

        if isinstance(inst,BaseRaw):
            return RawArray(inst_data,inst_info)
        else:
            return EpochsArray(inst_data,inst_info,events=inst[0].events,tmin=inst[0].tmin)

    def findICA(self,eogs=None,ecgs=None,event=None,preprocessed=False,ar=False,
                keyword_in=None,keyword_out=None,n_components=None,
                l_freq=None,h_freq=40,detrend=1,component_optimization_n=3,
                vis_tmin=None,vis_tmax=None,seed=11,overwrite=False):
        # keyword_out functionality was added so that ICA can be computed on
        # one raw data and applied to another
        # note: filter only filters evoked
        if os.path.isfile(self._fname('ica','ica','.fif',keyword_out)) and not overwrite:
            raise ValueError('ICA already calculated, use \'overwrite=True\' ' +
                             'to recalculate.')
        if event is None:
            inst = self._load_raw(preprocessed=preprocessed,keyword=keyword_in)
        else:
            inst = self._load_epochs(event,ar=ar,keyword=keyword_in)
        eogs,ecgs = self._default_aux(inst,eogs,ecgs)
        if not all([ch in inst.ch_names for ch in eogs + ecgs]):
            raise ValueError('Auxillary channels not in channel list.')
        if n_components is None:
            n_components = inst.estimate_rank()

        data_types = ['grad','mag']*self.meg + ['eeg']*self.eeg
        ica_insts = []
        for dt in data_types:
            ica = ICA(method='fastica',n_components=n_components,
                      random_state=seed)
            inst2 = inst.copy().pick_types(meg=False if dt == 'eeg' else dt,
                                           eeg=(dt == 'eeg'))
            ica.fit(inst2)
            fig = ica.plot_components(picks=np.arange(ica.n_components),
                                      show=False)
            kw = dt if keyword_out is None else dt + '_' + keyword_out
            fig.savefig(self._fname('plots','components','.jpg',kw))
            plt.close(fig)

            if isinstance(inst,BaseRaw):
                raw = inst.copy().pick_types(meg=False if dt == 'eeg' else dt,
                                             eeg=(dt == 'eeg'),eog=True,ecg=True)
                all_scores = self._make_ICA_components(raw,ica,eogs,ecgs,detrend,
                                                       l_freq,h_freq,kw,
                                                       vis_tmin,vis_tmax)
            '''if component_optimization_n:
                ica = self._optimize_components(raw,ica,all_scores,
                                                component_optimization_n,
                                                keyword_in,kw)'''
            inst2 = ica.apply(inst2, exclude=ica.exclude)
            self._save_ICA(ica,keyword=kw)
            ica_insts.append(inst2)
        ica_insts.append(inst.copy().pick_types(meg=False,eeg=False,eog=True,ecg=True,stim=True))

        inst = self._combine_insts(ica_insts)

        if isinstance(inst,BaseRaw):
            self._save_raw_preprocessed(inst,ica=(keyword_out is None),
                                        keyword=keyword_out)
        else:
            self._save_epochs(inst,keyword=
                (keyword_out if keyword_out is not None else 'ica'))

    def _optimize_components(self,raw,ica,all_scores,component_optimization_n,keyword,kw):
        # get component_optimization_n of components
        components = []
        for ch in all_scores:
            current_scores = list(all_scores[ch])
            for n in range(component_optimization_n):
                component = current_scores.index(max(current_scores))
                components.append(component)
                current_scores.pop(component)
        # get ways to combine the components
        def int2bin(n,i):
            b = []
            for j in range(n-1,-1,-1):
                if i/(2**j):
                    i -= 2**j
                    b.append(1)
                else:
                    b.append(0)
            return list(reversed(b))
        combinations = [int2bin(len(components),i) for i in range(2**len(components))]
        min_score = None
        evokeds = {}
        for ch in all_scores:
            if kw:
                evokeds[ch] = self._load_evoked('ica_%s_%s' %(ch,kw),keyword=keyword)
            else:
                evokeds[ch] = self._load_evoked('ica_%s' %(ch),keyword=keyword)
                print('Testing ICA component combinations for minimum correlation to artifact epochs')
        for combo in tqdm(combinations):
            score = 0
            ica.exclude = [component for i,component in enumerate(components) if combo[i]]
            for ch in all_scores:
                evoked = ica.apply(evokeds[ch].copy(),exclude=ica.exclude)
                sfreq = int(evoked.info['sfreq'])
                evoked_data = evoked.data[:,sfreq/10:-sfreq/10]
                for i in range(evoked_data.shape[0]):
                    evoked_data[i] -= np.median(evoked_data[i])
                score += abs(evoked_data).sum()*evoked_data.std(axis=0).sum()
            if min_score is None or score < min_score:
                best_combo = combo
                min_score = score
        ica.exclude = [component for i,component in enumerate(components) if best_combo[i]]
        return ica

    def _make_ICA_components(self,raw,ica,eogs,ecgs,detrend,l_freq,h_freq,
                             kw,vis_tmin,vis_tmax):
        if vis_tmin is not None:
            raw = raw.copy().crop(tmin=vis_tmin)
        if vis_tmax is not None:
            raw = raw.copy().crop(tmax=vis_tmax)
        all_scores = {}
        for ch in eogs:
            try:
                epochs = create_eog_epochs(raw, ch_name=ch)
            except:
                print('EOG %s dead' %(ch))
                continue
            indices, scores = ica.find_bads_eog(epochs, ch_name=ch)
            all_scores[ch] = scores
            if l_freq is not None or h_freq is not None:
                epochs = epochs.filter(l_freq=l_freq,h_freq=h_freq)
            evoked = epochs.average()
            if detrend is not None:
                evoked = evoked.detrend(detrend)
            self._save_evoked(evoked,'ica_%s' %(ch),keyword=kw)
            self._exclude_ICA_components(ica,ch,indices,scores)

        for ecg in ecgs:
            try:
                epochs = create_ecg_epochs(raw,ch_name=ecg)
            except:
                print('ECG %s dead' %(ecg))
                continue
            indices, scores = ica.find_bads_ecg(epochs)
            all_scores[ecg] = scores
            if l_freq is not None or h_freq is not None:
                epochs = epochs.filter(l_freq=l_freq,h_freq=h_freq)
            evoked = epochs.average()
            if detrend is not None:
                evoked = evoked.detrend(detrend)
            self._save_evoked(evoked,'ica_%s' %(ecg),keyword=kw)
            self._exclude_ICA_components(ica,ecg,indices,scores)
            return all_scores

    def _exclude_ICA_components(self,ica,ch,indices,scores):
        for ind in indices:
            if ind not in ica.exclude:
                ica.exclude.append(ind)
        print('Components removed for %s: ' %(ch) +
              ' '.join([str(i) for i in indices]))
        fig = ica.plot_scores(scores, exclude=indices, show=False)
        fig.savefig(self._fname('plots','source_scores','.jpg',ch))
        plt.close(fig)

    def plotICA(self,eogs=None,ecgs=None,preprocessed=False,event=None,ar=False,
                keyword_in=None,keyword_out=None,show=True):
        if event is None:
            inst = self._load_raw(preprocessed=preprocessed,keyword=keyword_in)
        else:
            inst = self._load_epochs(event,ar=ar,keyword=keyword_in)
        eogs,ecgs = self._default_aux(inst,eogs,ecgs)
        data_types = ['grad','mag']*self.meg + ['eeg']*self.eeg
        ica_insts = []
        for dt in data_types:
            inst2 = inst.copy().pick_types(meg=False if dt == 'eeg' else dt,
                                           eeg=(dt == 'eeg'))
            kw = dt if keyword_out is None else dt + '_' + keyword_out
            ica = self._load_ICA(keyword=kw)
            if isinstance(inst,BaseRaw):
                for ch in eogs:
                    evoked = self._load_evoked('ica_%s' %(ch),keyword=kw)
                    self._plot_ICA_sources(ica,evoked,ch,show)
                for ecg in ecgs:
                    evoked = self._load_evoked('ica_%s' %(ecg),keyword=kw)
                    self._plot_ICA_sources(ica,evoked,ecg,show)
            fig = ica.plot_components(picks=np.arange(ica.n_components),
                                      show=False)
            fig.show()
            ica.plot_sources(inst2,block=show,show=show,title=self.subject)
            inst2 = ica.apply(inst2,exclude=ica.exclude)
            if isinstance(inst,BaseRaw):
                for ch in eogs:
                    evoked = self._load_evoked('ica_%s' %(ch),keyword=kw)
                    self._plot_ICA_overlay(ica,evoked,ch,show)
                for ecg in ecgs:
                    evoked = self._load_evoked('ica_%s' %(ecg),keyword=kw)
                    self._plot_ICA_overlay(ica,evoked,ecg,show)
            plt.show()
            ica_insts.append(inst2)
            self._save_ICA(ica,keyword=kw)
        ica_insts.append(inst.copy().pick_types(meg=False,eeg=False,eog=True,ecg=True,stim=True))

        inst = self._combine_insts(ica_insts)

        if isinstance(inst,BaseRaw):
            self._save_raw_preprocessed(inst,ica=(keyword_out is None),
                                        keyword=keyword_out)
        else:
            self._save_epochs(inst,keyword=
                (keyword_out if keyword_out is not None else 'ica'))

    def _plot_ICA_overlay(self,ica,evoked,ch,show):
        evoked = evoked.detrend(1)
        fig = ica.plot_overlay(evoked,show=False)
        fig.suptitle('%s %s' % (self.subject,ch))
        fig.savefig(self._fname('plots','ica_overlay','.jpg',ch))
        if show:
            fig.show()

    def _plot_ICA_sources(self,ica,evoked,ch,show):
        fig = ica.plot_sources(evoked,exclude=ica.exclude,show=False)
        fig.suptitle('%s %s' % (self.subject, ch))
        fig.savefig(self._fname('plots','ica_time_course','.jpg',ch))
        if show:
            fig.show()

    def getEvents(self,baseline=True):
        if baseline:
            return self.events.keys()
        else:
            return [event for event in self.events.keys() if event != 'Baseline']

    def _save_epochs(self,epochs,event,ar=False,keyword=None):
        print('Saving epochs for ' + event + ' autoreject'*ar +
              ' %s' %(keyword)*(keyword is not None))
        epochs.save(self._fname('epochs','epo','.fif',event,'ar'*ar,keyword))

    def _save_evoked(self,evoked,event,ar=False,keyword=None):
        print('Saving evoked for ' + event + ' autoreject'*ar +
              ' %s' %(keyword)*(keyword is not None))
        evoked.save(self._fname('evoked','ave','.fif',event,'ar'*ar,keyword))

    def _load_epochs(self,event,ar=False,keyword=None):
        fname = self._fname('epochs','epo','.fif',event,'ar'*ar,keyword)
        if not os.path.isfile(fname):
            raise ValueError(event + ' epochs must be made first' +
                             ' for autoreject'*ar +
                             ' for %s' %(keyword)*(keyword is not None))
        epochs = read_epochs(fname,verbose=False,preload=True)
        print('%s epochs loaded' %(event) + ' for autoreject'*ar +
              ' for %s' %(keyword)*(keyword is not None))
        return epochs

    def _load_evoked(self,event,ar=False,keyword=None):
        fname = self._fname('evoked','ave','.fif',event,'ar'*ar,keyword)
        if not os.path.isfile(fname):
            raise ValueError(event + ' evoked must be made first' +
                             ' for autoreject'*ar +
                             ' for %s' %(keyword)*(keyword is not None))
        evoked = read_evokeds(fname,verbose=False)
        print('%s epochs loaded' %(event) + ' for autoreject'*ar +
              ' for %s' %(keyword)*(keyword is not None))
        return evoked[0]

    def _load_autoreject(self,event):
        if os.path.isfile(self._fname('epochs','ar','.npz',event)):
            f = np.load(self._fname('epochs','ar','.npz',event))
            return f['ar'].item(),f['reject_log'].item()
        else:
            print('Autoreject must be run for ' + event)

    def _save_autoreject(self,event,ar,reject_log):
        np.savez_compressed(self._fname('epochs','ar','.npz',event),ar=ar,
                            reject_log=reject_log)

    def _save_TFR(self,tfr,frequencies,n_cycles,
                 event,condition,value,keyword,compressed=True):
       print('Saving TFR for %s %s %s' %(event,condition,value))
       if compressed:
           np.savez_compressed(self._fname('TFR','tfr','.npz',event,condition,
                                           value,keyword),
                               tfr=tfr,frequencies=frequencies,
                               n_cycles=n_cycles)
       else:
           np.save(self._fname('TFR','tfr','.npy',event,condition,value,keyword),
                   tfr)
           np.savez_compressed(self._fname('TFR','tfr_params','.npz',
                                           event,condition,value,keyword),
                               frequencies=frequencies,n_cycles=n_cycles)

    def _load_TFR(self,event,condition,value,keyword=None):
       fname = self._fname('TFR','tfr','.npy',event,condition,value,keyword)
       fname1b = self._fname('TFR','tfr_params','.npz',event,condition,value,
                             keyword)
       fname2 = self._fname('TFR','tfr','.npz',event,condition,value,keyword)
       if os.path.isfile(fname) and os.path.isfile(fname1b):
           tfr = np.load(fname)
           f = np.load(fname1b)
           frequencies,n_cycles = f['frequencies'],f['n_cycles']
       elif os.path.isfile(fname2):
           f = np.load(fname2)
           tfr,frequencies,n_cycles = f['tfr'],f['frequencies'],f['n_cycles']
       else:
           raise ValueError('No TFR to load for %s %s %s'
                            %(event,condition,value))
       print('TFR loaded for %s %s %s' %(event,condition,value))
       return tfr,frequencies,n_cycles

    def _save_CPT(self,event,condition,value,clusters,cluster_p_values,
                  times,frequencies=None,band=None):
        print('Saving CPT for %s %s %s' %(event,condition,value))
        if band:
            np.savez_compressed(self._fname('CPT','CPT','.npz',event,condition,
                                            value,band),
                                clusters=clusters,
                                cluster_p_values=cluster_p_values,band=band)
        elif frequencies:
            np.savez_compressed(self._fname('CPT','CPT','.npz',event,condition,
                                            value,'tfr'),
                                clusters=clusters,frequencies=frequencies,
                                cluster_p_values=cluster_p_values)
        else:
            np.savez_compressed(self._fname('CPT','CPT','.npz',event,condition,
                                            value),
                                clusters=clusters,
                                cluster_p_values=cluster_p_values)

    def _load_CPT(self,event,condition,value,tfr=False,band=None):
        if band:
            fname = self._fname('CPT','CPT','.npz',event,condition,value,band)
        elif tfr:
            fname = self._fname('CPT','CPT','.npz',event,condition,value,'tfr')
        else:
            fname = self._fname('CPT','CPT','.npz',event,condition,value)
        if os.path.isfile(fname):
            f = np.load(fname)
            print('Cluster permuation test loaded for %s %s %s'
                  %(event,condition,value))
            if band:
                return f['clusters'],f['cluster_p_values'],f['band']
            elif tfr:
                return f['clusters'],f['cluster_p_values'],f['frequencies']
            else:
                return f['clusters'],f['cluster_p_values']
        else:
            raise ValueError('Cluster permuation test not found for %s %s %s'
                             %(event,condition,value))

    def _save_inverse(self,inv,lambda2,method,pick_ori,
                      event,condition,value,ar=False,keyword=None):
        print('Saving inverse for %s %s %s' %(event,condition,value))
        write_inverse_operator(self._fname('sources','inv','.fif','ar'*ar,
                                           keyword,event,condition,value),
                               inv,verbose=False)
        np.savez_compressed(self._fname('sources','inverse_params','.npz',
                                        'ar'*ar,keyword,event,condition,value),
                            lambda2=lambda2,method=method,pick_ori=pick_ori)

    def _load_inverse(self,event,condition,value,ar=False,keyword=None):
        fname = self._fname('sources','inv','.fif','ar'*ar,keyword,event,
                            condition,value)
        fname2 = self._fname('sources','inverse_params','.npz','ar'*ar,
                            keyword,event,condition,value)
        if os.path.isfile(fname) and os.path.isfile(fname2):
            f = np.load(fname2)
            return (read_inverse_operator(fname),f['lambda2'].item(),
                    f['method'].item(),f['pick_ori'].item())
        else:
            print('Inverse not found for %s %s %s' %(event,condition,value))

    def _save_source(self,stc,event,condition,value,ar=False,keyword=None,
                     fs_av=False):
        if fs_av:
            print('Saving source fs average for %s %s %s' %(event,condition,
                                                            value))
            stc.save(self._fname('sources','source',None,'ar'*ar,keyword,event,
                                 condition,value,'fs_av'),ftype='stc')
        else:
            print('Saving source for %s %s %s' %(event,condition,value))
            stc.save(self._fname('sources','source',None,'ar'*ar,keyword,event,
                                 condition,value),ftype='stc')

    def _load_source(self,event,condition,value,fs_av=False,ar=False,
                     keyword=None):
        fname = self._fname('sources','source-lh','.stc','ar'*ar,keyword,event,
                            condition,value,'fs_av'*fs_av)
        if os.path.isfile(fname):
            print('Fs average s'*fs_av + 'S'*(not fs_av) + 'ource loaded for '+
                  '%s %s %s' %(event,condition,value))
            return read_source_estimate(fname)
        else:
            print('Source not found for %s %s %s' %(event,condition,value))

    def _save_PSD_image(self,image,preprocessed,ica,keyword,ch,N,deltaN,
                        fmin,fmax,NW):
        print('Saving psd multitaper image')
        np.savez_compressed(self._fname('psd_multitaper','image','.npz',
                            'preprocessed'*preprocessed,'ica'*ica,ch,
                            'N_%i_dN_%.2f' %(N,deltaN),
                            'fmin_%.2f_fmax_%.2f_NW_%i' %(fmin,fmax,NW)),
                            image=image)

    def _load_PSD_image(self,preprocessed,ica,keyword,ch,N,deltaN,fmin,fmax,NW):
        fname = self._fname('psd_multitaper','image','.npz',
                            'preprocessed'*preprocessed,'ica'*ica,ch,
                            'N_%i_dN_%.2f' %(N,deltaN),
                            'fmin_%.2f_fmax_%.2f_NW_%i' %(fmin,fmax,NW))
        if os.path.isfile(fname):
            print('Loading image')
            return np.load(fname)['image']
        else:
            return None

    def autoMarkBads(self,preprocessed=False,keyword_in=None,keyword_out=None,
                     flat=dict(grad=1e-11, # T / m (gradiometers)
                               mag=5e-13, # T (magnetometers)
                               eeg=2e-5, # V (EEG channels)
                               ),
                     reject=dict(grad=5e-10, # T / m (gradiometers)
                                 mag=1e-11, # T (magnetometers)
                                 eeg=5e-4, # V (EEG channels)
                                 ),
                     bad_seeds=0.25,seeds=1000,datalen=1000,
                     overwrite=False):
        # now we will use seeding to remove bad channels
        keyword_out = keyword_out if not keyword_out is None else keyword_in
        if (os.path.isfile(self._fname('raw_preprocessed','raw','.fif',keyword_out)) and
            not overwrite):
           print('Raw data already marked for bads, use \'overwrite=True\'' +
                 ' to recalculate.')
           return
        raw = self._load_raw(preprocessed=preprocessed,keyword=keyword_in)
        data_types = ['grad','mag']*self.meg + ['eeg']*self.eeg
        bads = []
        rawlen = len(raw._data[0])
        for dt in data_types:
            print(dt)
            raw2 = raw.copy().pick_types(meg=dt if dt in ['grad','mag'] else False,
                                         eeg=dt == 'eeg')
            for i in range(len(raw2.ch_names)):
                flat_count, reject_count = 0, 0
                for j in range(seeds):
                    start = np.random.randint(0,rawlen-datalen)
                    seed = raw2._data[i, start:start+datalen]
                    min_c = seed.min()
                    max_c = seed.max()
                    diff_c = max_c - min_c
                    if diff_c < flat[dt]:
                        flat_count += 1
                    if diff_c > reject[dt]:
                        reject_count += 1
                if flat_count > (seeds * bad_seeds):
                    bads.append(raw2.ch_names[i])
                    print(raw2.ch_names[i] + ' removed: flat')
                elif reject_count > (seeds * bad_seeds):
                    bads.append(raw2.ch_names[i])
                    print(raw2.ch_names[i] + ' removed: reject')

        raw.info['bads'] = bads
        self._save_raw_preprocessed(raw,keyword=keyword_out)

    def closePlots(self):
        plt.close('all')

    def plotRaw(self,n_per_screen=20,scalings=None,preprocessed=False,
                ica=False,keyword=None,l_freq=0.5,h_freq=40,
                interpolate_bads=True,overwrite=False):
        if (os.path.isfile(self._fname('raw_preprocessed','raw','.fif',keyword))
            and not overwrite):
            print('Use overwrite = True to overwrite')
            return
        raw = self._load_raw(preprocessed=preprocessed,ica=ica,keyword=keyword)
        bads_ind = [raw.info['ch_names'].index(ch) for ch in raw.info['bads']]
        this_chs_ind = list(pick_types(raw.info,meg=self.meg,eeg=self.eeg)) + bads_ind
        aux_chs_ind = list(pick_types(raw.info,meg=False,eog=True,ecg=True))
        order = []
        n = n_per_screen-len(aux_chs_ind)
        for i in range(len(this_chs_ind)//n+1):
            order.append(this_chs_ind[i*n:min([len(this_chs_ind),(i+1)*n])])
            order.append(aux_chs_ind)
        order = np.concatenate(order)
        if self.eeg:
            raw.set_eeg_reference(ref_channels=[],projection=False)
        elif self.meg:
            order = None
        raw2 = raw.copy().filter(l_freq=l_freq,h_freq=h_freq)
        raw2.plot(show=True, block=True, color=dict(eog='steelblue'),
                 title="%s Bad Channel Selection" % self.subject, order=order,
                 scalings=scalings)
        raw.info['bads'] = raw2.info['bads']
        if interpolate_bads:
            raw = raw.interpolate_bads(reset_bads=True)
        self._save_raw_preprocessed(raw,ica=ica,keyword=keyword)

    def makeEpochs(self,preprocessed=False,ica=False,keyword_in=None,
                   keyword_out=None,detrend=0,normalized=True,overwrite=False):
        if (all([os.path.isfile(self._fname('epochs','epo','.fif',event,
                                            keyword_out))
                 for event in self.events]) and not overwrite):
            print('Epochs already made, use \'overwrite=True\' to recalculate.')
            return
        raw = self._load_raw(preprocessed=preprocessed,ica=ica,
                             keyword=keyword_in)

        include = [i for i in range(self.n) if not (i in self.no_response or
                   i in self.exclude_response)]
        if normalized:
            # make baseline epochs
            baseline_ch,tmin,tmax = self.events['Baseline']
            events = find_events(raw,stim_channel=baseline_ch,
                                 output="onset",verbose=False)
            events = events[include,:]
            events[:,2] = include

            bl_epochs = Epochs(raw,events,tmin=tmin-self.tbuffer,
                               tmax=tmax+self.tbuffer,baseline=None,verbose=False,
                               detrend=detrend,preload=True)
            self._save_epochs(bl_epochs,'Baseline',keyword=keyword_out)

            baseline_data = bl_epochs.crop(tmin=tmin,tmax=tmax).get_data()
            baseline_arr = baseline_data.mean(axis=2)

        include_response = [i for i in range(self.n) if
                            i not in self.exclude_response]
        include_response = include_response[:-len(self.no_response) or None]

        for event in self.getEvents(baseline=False):
            event_ch,tmin,tmax = self.events[event]
            events = find_events(raw,stim_channel=event_ch,output="onset",
                                 verbose=False)
            print('%s events found: %i' %(event,len(events)))

            expected_length = self.n-(event=='Response')*len(self.no_response)
            if len(events) != expected_length:
                raise ValueError('Mismatching # of stimulus presentations ' +
                                 'found for %s ' %(event) +
                                  '%s EEG Events ' %(len(events))*self.eeg +
                                  '%s MEG Events ' %(len(events))*self.meg +
                                  '%s Behavior Events ' %(expected_length))

            if event == 'Response': #assumes no response if excluded
                print('No response trials given: %i' %(len(self.no_response)))
                events = events[include_response,:]
            else:
                events = events[include,:]

            events[:,2] = include

            epochs = Epochs(raw,events,tmin=tmin-self.tbuffer,
                            tmax=tmax+self.tbuffer,proj=False,preload=True,
                            baseline=None,verbose=False, detrend=detrend,
                            reject_by_annotation=False)
            if self.eeg:
                epochs = epochs.set_eeg_reference(ref_channels='average',
                                                  projection=False)
            if normalized:
                epochs_data = epochs.get_data()
                info = epochs.info
                epochs_demeaned_data = np.array([arr - baseline_arr.T
                                                 for arr in epochs_data.T]).T
                epochs = EpochsArray(epochs_demeaned_data,info,
                                     events=events,verbose=False,
                                     proj=False,tmin=tmin-self.tbuffer)
            self._save_epochs(epochs,event,keyword=keyword_out)

    def demeanEpochs(self,event,condition,values=None,ar=False,keyword_in=None,
                     keyword_out=None):
        values = self._default_values(values,condition)
        bl_epochs = self._load_epochs('Baseline',ar=ar,keyword=keyword_in)

        bl_value_indices = self._get_indices(bl_epochs,condition,values)
        bl_values_dict = self._get_data(bl_epochs,values,bl_value_indices,tmin=None,
                                        tmax=None,mean_and_std=False)

        epochs = self._load_epochs(event,ar=ar,keyword=keyword_in)
        value_indices = self._get_indices(epochs,condition,values)
        epochs_data = epochs.get_data()

        for value in values:
            baseline_data = bl_values_dict[value]
            baseline_arr = baseline_data.mean(axis=0).mean(axis=1) #average over epochs and times
            indices = value_indices[value]
            baseline_arr = np.tile(baseline_arr[np.newaxis,:,np.newaxis],
                                   (len(indices),1,epochs_data.shape[2]))
            epochs_data[indices] = epochs_data[indices] - baseline_arr
        event_ch,tmin,tmax = self.events[event]

        epochs_demeaned = EpochsArray(epochs_data,epochs.info,
                                      events=epochs.events,verbose=False,
                                      proj=False,tmin=epochs.tmin)
        self._save_epochs(epochs_demeaned,event,keyword=keyword_out)
        self._save_epochs(bl_epochs,'Baseline',keyword=keyword_out)

    def plotEpochs(self,event,n_epochs=20,n_channels=20,scalings=None,
                   l_freq=None,h_freq=None,ar=False,keyword_in=None,
                   keyword_out=None):
        # note: if linear trend, apply l_freq filter
        keyword_out = keyword_in if keyword_out is None else keyword_out
        epochs = self._load_epochs(event,ar=ar,keyword=keyword_in)
        if l_freq is not None or h_freq is not None:
            epochs_copy = epochs.copy().filter(l_freq=l_freq,h_freq=h_freq)
        if len(epochs.event_id) != len(epochs):
            event_id = epochs.event_id
            epochs.event_id = {str(i):i for i in range(len(epochs))}
            epochs_copy.plot(n_epochs=n_epochs,n_channels=n_channels,block=True,
                             scalings=scalings)
            epochs.event_id = event_id
        else:
            epochs_copy.plot(n_epochs=n_epochs,n_channels=n_channels,block=True,
                             scalings=scalings)
        epochs.info['bads'] = epochs_copy.info['bads']
        epochs.events = epochs_copy.events
        epochs.selection = epochs_copy.selection
        epochs.drop_log = epochs_copy.drop_log
        epochs._data = epochs._data[epochs.selection]
        self._save_epochs(epochs,event,ar=ar,keyword=keyword_out)

    def plotTopo(self,event,condition=None,values=None,
                 epochs=None,ar=False,keyword=None,
                 ylim={'eeg':[-10,20]},l_freq=None,h_freq=None,
                 tmin=None,tmax=None,detrend=1,comparison=False,
                 seed=11,downsample=True,show=True):
        epochs = self._prepare_epochs(event,epochs,ar,keyword,
                                      tmin,tmax,l_freq,h_freq)
        if comparison:
            values = self._default_values(values,condition)
            value_indices = self._get_indices(epochs,condition,values)
            if downsample:
                np.random.seed(seed)
                nTR = min([len(value_indices[value]) for value in value_indices])
        else:
            values = ['all']
            value_indices = {'all':[]}
        fig,axs = plt.subplots((2*self.meg+self.eeg),len(values),
                               figsize=(5*len(values),5*(2*self.meg+self.eeg)))
        if not isinstance(axs,np.ndarray):
            axs = np.array([axs])
        for i,value in enumerate(values):
            if not value in value_indices:
                continue
            indices = value_indices[value]
            if comparison:
                ax = axs[i] if axs.ndim == 1 else axs[0,i]
                if self.meg:
                    ax2 = axs[1,i]
            else:
                ax = axs[0]
                if self.meg:
                    ax2 = axs[1]
            if comparison and downsample:
                print('Subsampling %i/%i %s %s.' %(nTR,len(indices),condition,
                                                   value))
                np.random.shuffle(indices)
                indices = indices[:nTR]
            if value == 'all':
                evoked = epochs.average()
            else:
                evoked = epochs[indices].average()
            if detrend:
                evoked = evoked.detrend(order=detrend)
            if self.meg:
                evoked.plot_topo(axes=[ax,ax2],show=False,ylim=ylim)
            else:
                evoked.plot_topo(axes=ax,show=False,ylim=ylim)
            ax.set_title('%s %s %s %s' %(self.subject,event,condition,value))
        fname = self._fname('plots','evoked','.jpg',
                            'ar'*ar,keyword,event,condition,
                            *value_indices.keys())
        fig.savefig(fname)
        self._show_fig(fig,show)


    def plotTopomapBands(self,event,condition,values=None,ar=False,keyword=None,
                         tfr_keyword=None,contrast=False,tmin=None,tmax=None,
                         tfr=True,bands={'theta':(4,8),'alpha':(8,15),'beta':(15,30)},
                         vmin=None,vmax=None,contours=6,time_points=5,show=True):
        for band in bands:
            self.plotTopomap(event,condition,values=values,ar=ar,keyword=keyword,
                             contrast=contrast,tmin=tmin,tmax=tmax,tfr=True,
                             tfr_keyword=tfr_keyword,
                             band_struct=(band,bands[band][0],bands[band][1]),
                             vmin=vmin,vmax=vmax,contours=contours,
                             time_points=time_points,show=show)

    def plotTopomap(self,event,condition,values=None,ar=False,keyword=None,
                    tfr_keyword=None,contrast=False,tmin=None,tmax=None,tfr=False,
                    band_struct=None,vmin=None,vmax=None,
                    contours=6,time_points=5,show=True):
        epochs = self._load_epochs(event,ar=ar,keyword=keyword)
        values = self._default_values(values,condition,contrast)
        value_indices = self._get_indices(epochs,condition,values)
        tmin,tmax = self._default_t(event,tmin,tmax)
        times = self._get_times(epochs,event,tmin=tmin,tmax=tmax)
        info = epochs.info
        band_title = '%s ' %(band_struct[0]) if band_struct is not None else ''
        if tfr:
            tind = np.array([i for i,t in enumerate(times) if
                             t >= tmin and t<=tmax])
            values_dict,frequencies = \
                self._get_tfr_data(event,condition,values,tfr_keyword,value_indices,
                                   tind,band=band_struct,mean_and_std=False,
                                   band_mean=False)
        else:
            values_dict = self._get_data(epochs,values,value_indices,tmin,tmax,
                                         mean_and_std=False)
            frequencies = None
        if contrast:
            fig,axes = plt.subplots(1,time_points+(not tfr))
            fig.suptitle(band_title + '%s %s Contrast' %(values[0],values[1]))
            epochs_0 = values_dict[values[0]]
            epochs_1 = values_dict[values[1]]
            if tfr:
                nave = min([epochs_0.shape[0],epochs_1.shape[0]])
                epochs_0 = np.swapaxes(epochs_0,2,3)
                epochs_1 = np.swapaxes(epochs_1,2,3)
                tfr_con_data = epochs_1.mean(axis=0) - epochs_0.mean(axis=0)
                evo_con = AverageTFR(info,tfr_con_data,times,frequencies,nave)
                dt = (tmax-tmin)/time_points
                for i,t in enumerate(np.linspace(tmin,tmax,time_points)):
                    evo_con.plot_topomap(colorbar=True if i == time_points-1 else False,
                                     vmin=vmin,vmax=vmax,contours=contours,axes=axes[i],
                                     title='time=%0.1f' %(t),tmin=t-dt/2,tmax=t+dt/2,show=False)
            else:
                evo_con_data = evo_1.mean(axis=0) - evo_0.mean(axis=0)
                evo_con = EvokedArray(evo_con_data,info,tmin=tmin)
                evo_con.plot_topomap(colorbar=True,vmin=vmin,vmax=vmax,
                                     contours=contours,axes=axes,show=False)
            fig.savefig(self._fname('plots','topo','.jpg',event,condition,
                                    values[0],values[1],
                                    '' if band_struct is None else band_struct[0]))
            self._show_fig(fig,show)
        else:
            for i,value in enumerate(values):
                fig,axes = plt.subplots(1,time_points+(not tfr))
                fig.suptitle(band_title + '%s %s' %(condition,value))
                epochs_data = values_dict[value]
                if tfr:
                    nave = epochs_data.shape[0]
                    evo_data = np.swapaxes(epochs_data,2,3).mean(axis=0)
                    evo = AverageTFR(info,evo_data,times,frequencies,nave)
                    dt = (tmax-tmin)/time_points
                    for i,t in enumerate(np.linspace(tmin,tmax,time_points)):
                        evo.plot_topomap(colorbar=True if i == time_points-1 else False,
                                         vmin=vmin,vmax=vmax,contours=contours,axes=axes[i],
                                         title='time=%0.1f' %(t),tmin=t-dt/2,tmax=t+dt/2,show=False)
                else:
                    evo = EvokedArray(epochs_data.mean(axis=0),info,tmin=tmin)
                    evo.plot_topomap(colorbar=True,vmin=vmin,vmax=vmax,
                                     contours=contours,axes=axes,show=False)
                fig.savefig(self._fname('plots','topo','.jpg',event,condition,
                                        value,'' if band_struct is None else band_struct[0]))
                self._show_fig(fig,show)


    def dropEpochsByBehaviorIndices(self,bad_indices,event=None,ar=False,
                                    keyword_in=None,keyword_out=None):
        if event is None:
            for event in self.events:
                self.dropEpochsByBehaviorIndices(bad_indices,event=event,
                                                 ar=ar,keyword_in=keyword_in,
                                                 keyword_out=keyword_out)
        epochs = self._load_epochs(event,ar=ar,keyword=keyword_in)
        good_indices = [i for i in range(self.n) if i not in bad_indices]
        epochs_indices = self._behavior_to_epochs_indices(epochs,good_indices)
        if keyword_out:
            ar=False
        self._save_epochs(epochs[epochs_indices],event,ar=ar,
                          keyword=keyword_out)

    def markBadChannels(self,bad_channels,event=None,ar=False,keyword_in=None,
                        keyword_out=None,preprocessed=False,ica=False):
        keyword_out = keyword_in if keyword_out is None else keyword_out
        if event is None:
            raw = self._load_raw(preprocessed=preprocessed,ica=ica,
                                 keyword=keyword_in)
            raw.info['bads'] += bad_channels
            self._save_raw_preprocessed(raw,ica=ica,keyword=keyword_out)
        else:
            if event is 'all':
                for event in self.events:
                    self.markBadChannels(bad_channels,event=event,ar=ar,
                                         keyword_in=keyword_in,
                                         keyword_out=keyword_out)
            epochs = self._load_epochs(event,ar=ar,keyword=keyword_in)
            epochs.info['bads'] += bad_channels
            if keyword_out:
                ar=False
            self._save_epochs(epochs,event,ar=ar,keyword=keyword_out)

    def alignBaselineEpochs(self,event,ar=False,keyword=None):
        epochs = self._load_epochs(event,ar=ar,keyword=keyword)
        bl_epochs = self._load_epochs('Baseline',ar=ar,keyword=keyword)
        exclude = [i for i in range(len(bl_epochs)) if
                   i not in epochs.selection]
        bl_epochs.drop(exclude)
        self._save_epochs(bl_epochs,'Baseline',ar=ar,keyword=keyword)

    def plotEvoked(self,event,condition=None,values=None,
                   epochs=None,ar=False,keyword=None,image=True,
                   ylim={'eeg':[-10,20]},l_freq=None,h_freq=None,
                   tmin=None,tmax=None,detrend=1,seed=11,downsample=True,
                   picks=None,show=True):
        epochs = self._prepare_epochs(event,epochs,ar,keyword,
                                      tmin,tmax,l_freq,h_freq)
        if condition is not None:
            values = self._default_values(values,condition)
            value_indices = self._get_indices(epochs,condition,values)
            if downsample:
                np.random.seed(seed)
                nTR = min([len(value_indices[value]) for value in value_indices])
        else:
            values = ['all']
            value_indices = {'all':[]}
            nTR = len(epochs)

        if picks is not None:
           picks = pick_types(epochs.info,meg=False, eog=False,include=picks)

        x_dim = (1+image)*(2*self.meg+self.eeg)
        y_dim = len(values)
        fig,axs = plt.subplots(x_dim,y_dim,figsize=(5*y_dim,5*x_dim))
        if not isinstance(axs,np.ndarray):
            axs = np.array([axs])
        for i,value in enumerate(values):
            if not value in value_indices:
                continue
            if value == 'all':
                evoked = epochs.average()
                indices = range(len(epochs))
            else:
                indices = value_indices[value]
                if condition is not None and downsample:
                    print('Subsampling %i/%i %s %s.' %(nTR,len(indices),condition,
                                                       value))
                    np.random.shuffle(indices)
                    indices = indices[:nTR]
                evoked = epochs[indices].average()
            if detrend:
                evoked = evoked.detrend(order=detrend)
            if y_dim > 1:
                axs2 = axs[:,i]
            else:
                axs2 = axs

            axs3 = ([axs2[0], axs2[1], axs2[2]] if self.meg and self.eeg else
                    [axs2[0]] if self.eeg else [axs2[0], axs2[1]])
            evoked.plot(axes=axs3,show=False,ylim=ylim,picks=picks)
            axs2[0].set_title('%s %s %s %s' %(self.subject,event,condition,value) +
                         (', %i trials used'%(len(indices))))
            if image:
                axs3 = ([axs2[3], axs2[4], axs2[5]] if self.meg and self.eeg else
                    [axs2[1]] if self.eeg else [axs2[2], axs2[3]])
                evoked.plot_image(axes=axs3,show=False,clim=ylim,picks=picks)
        fname = self._fname('plots','evoked','.jpg',
                            'ar'*ar,keyword,event,condition,
                            *value_indices.keys())
        fig.savefig(fname)
        self._show_fig(fig,show)

    def _show_fig(self,fig,show):
        if show:
            fig.show()
        else:
            plt.close(fig)

    def _prepare_epochs(self,event,epochs,ar,keyword,tmin,tmax,
                        l_freq,h_freq):
        tmin,tmax = self._default_t(event,tmin,tmax)
        if epochs is None:
            epochs = self._load_epochs(event,ar=ar,keyword=keyword)
        else:
            epochs = epochs.copy()
        epochs = epochs.pick_types(meg=self.meg,eeg=self.eeg)
        if l_freq is not None or h_freq is not None:
            epochs = epochs.filter(l_freq=l_freq,h_freq=h_freq)
        epochs = epochs.crop(tmin=tmin,tmax=tmax)
        return epochs

    def _default_t(self,event,tmin,tmax):
        if tmin is None:
            _,tmin,_ = self.events[event]
        if tmax is None:
            _,_,tmax = self.events[event]
        if type(tmin) is str:
            condition = tmin
            if condition in self.behavior:
                tmin = {}
                for i,value in enumerate(self.behavior[condition]):
                    tmin[i] = -value
            else:
                raise ValueError('tmin must be a int/float or condition')
        if type(tmax) is str:
            condition = tmax
            if condition in self.behavior:
                tmax = {}
                for i,value in enumerate(self.behavior[condition]):
                    tmax[i] = value
            else:
                raise ValueError('tmax must be a int/float or condition')
        return tmin,tmax

    def _default_vs(self,epochs_mean,epochs_std,vmin,vmax):
        if vmin is None:
            vmin = (epochs_mean-epochs_std).min()
        if vmax is None:
            vmax = (epochs_mean+epochs_std).max()
        return vmin,vmax

    def _behavior_to_epochs_indices(self,epochs,indices):
        return [self._behavior_to_epochs_index(epochs,i) for i in indices if
                self._behavior_to_epochs_index(epochs,i)]

    def _behavior_to_epochs_index(self,epochs,ind):
        if ind in epochs.events[:,2]:
            return list(epochs.events[:,2]).index(ind)

    def _get_binned_indices(self,epochs,condition,bins):
        bin_indices = {}
        h,edges = np.histogram([cd for cd in self.behavior[condition] if not
                                np.isnan(cd)],bins=bins)
        for j in range(1,len(edges)):
            indices = [i for i in range(self.n) if
                       self.behavior[condition][i] >= edges[j-1] and
                       self.behavior[condition][i] <= edges[j]]
            name = '%.2f-%.2f, count %i' %(edges[j-1],edges[j],len(indices))
            bin_indices[name] = self._behavior_to_epochs_indices(epochs,indices)
        return bin_indices

    def _get_indices(self,epochs,condition,values):
        value_indices = {}
        if len(values) > 4 and all([istype(val,int) or istype(val,float)
                                        for val in values]):
            binsize = float(value[1] - value[0])
        for value in values:
            if len(values) > 4 and all([istype(val,int) or istype(val,float)
                                        for val in values]):
                indices = [i for i in range(self.n) if
                           self.behavior[condition][i] >= value - binsize/2 and
                           value + binsize/2 >= self.behavior[condition][i]]
            else:
                indices = [i for i in range(self.n) if
                           self.behavior[condition][i] == value]
            epochs_indices = self._behavior_to_epochs_indices(epochs,indices)
            if epochs_indices:
                value_indices[value] = epochs_indices
        value_indices['all'] = [i for value in value_indices for i in value_indices[value]]
        return value_indices

    def channelPlot(self,event,condition,values=None,ar=False,keyword=None,
                    butterfly=False,contrast=False,aux=False,
                    tmin=None,tmax=None,vmin=None,vmax=None,show=True):
        self._plotter_main(event,condition,values,butterfly=butterfly,
                           contrast=contrast,aux=aux,ar=ar,keyword=keyword,
                           tmin=tmin,tmax=tmax,vmin=vmin,vmax=vmax,show=show)

    def plotTFR(self,event,condition,values=None,ar=False,keyword=None,
                tfr_keyword=None,contrast=False,butterfly=False,aux=False,
                bands={'theta':(4,8),'alpha':(8,15),'beta':(15,30)},
                tmin=None,tmax=None,vmin=None,vmax=None):
        # computes the time frequency representation of a particular event and
        # condition or all events and conditions
        # default values are frequency from 3 to 35 Hz with 32 steps and
        # cycles from 3 to 10 s-1 with 32 steps
        if bands:
            for band in bands:
                print(band + ' band')
                fmin,fmax = bands[band]
                band_struct = (band,fmin,fmax)
                self._plotter_main(event,condition,values,contrast=contrast,
                                   aux=aux,ar=ar,keyword=keyword,
                                   butterfly=butterfly,tfr=True,
                                   band=band_struct,tfr_keyword=tfr_keyword,
                                   tmin=tmin,tmax=tmax,vmin=vmin,vmax=vmax)
        else:
            values = self._default_values(values,condition,contrast)
            for value in values:
                self._plotter_main(event,condition,[value],contrast=contrast,
                                   aux=aux,ar=ar,keyword=keyword,
                                   butterfly=butterfly,tfr=True,band=None,
                                   tfr_keyword=tfr_keyword,tmin=tmin,tmax=tmax,
                                   vmin=vmin,vmax=vmax)

    def _setup_plot(self,ch_dict,butterfly=False,contrast=False,values=None):
        if butterfly:
            nplots = 1 if contrast else len(values)
            fig,ax_arr = plt.subplots(1,nplots)
            if len(values) == 1:
                ax_arr = [ax_arr]
        else:
            dim1 = int(np.ceil(np.sqrt(len(ch_dict))))
            dim2 = int(np.ceil(float(len(ch_dict))/dim1))
            fig, ax_arr = plt.subplots(dim1,dim2,sharex=True,sharey=True)
            fig.set_tight_layout(False)
            fig.subplots_adjust(left=0.1,right=0.9,top=0.9,bottom=0.1,
                                wspace=0.05,hspace=0.05)
            ax_arr = ax_arr.flatten()
            for i,ax in enumerate(ax_arr):
                ax.set_facecolor('white')
                ax.set_frame_on(False)
                if i % dim1:
                    ax.set_yticks([])
                if i < len(ax_arr)-dim2:
                    ax.set_xticks([])
        return fig, ax_arr

    def _get_ch_dict(self,inst,aux=False):
        if aux:
            chs = pick_types(inst.info,meg=False,eog=True,ecg=True)
        else:
            chs = pick_types(inst.info,meg=self.meg,eeg=self.eeg)
        return {ch:inst.ch_names[ch] for ch in chs}

    def _default_values(self,values,condition,contrast=False):
        if values is None:
            values = np.unique([cd for cd in self.behavior[condition] if
                                (type(cd) is str or type(cd) is np.string_ or
                                 type(cd) is np.str_ or not np.isnan(cd))])
            if (len(values) > 5 and
                all([istype(val,int) or istype(val,float) for val in values])):
               values,edges = np.histogram(values,bins=5)
        if type(contrast) is list and len(contrast) == 2:
            values = contrast
        elif contrast:
            values = [max(values),min(values)]
        return values

    def _get_tfr_data(self,event,condition,values,keyword,value_indices,tind,
                      band=None,mean_and_std=True, band_mean=True):
        values_dict = {}
        frequencies_old = None
        for value in values:
            epochs_data,frequencies,_ = self._load_TFR(event,condition,value,
                                                       keyword)
            epochs_data = np.swapaxes(epochs_data,2,3)
            if frequencies_old is not None and frequencies != frequencies_old:
                raise ValueError('TFRs must be compared for the same ' +
                                 'frequencies')
            if band is not None:
                band_name,fmin,fmax = band
                band_indices = [index for index in range(len(frequencies)) if
                                frequencies[index] >= fmin and
                                frequencies[index] <= fmax]
                epochs_std = \
                    np.sqrt(epochs_data[:,:,:,band_indices].mean(axis=3)**2 +
                            epochs_data[:,:,:,band_indices].std(axis=3)**2)
                epochs_data = epochs_data[:,:,:,band_indices]
                if band_mean:
                    epochs_data = epochs_data.mean(axis=3)
            if mean_and_std:
                if band is None:
                    epochs_std = np.sqrt(epochs_data.mean(axis=0)**2+
                                         epochs_data.std(axis=0)**2)
                else:
                    epochs_std = np.sqrt(epochs_std.mean(axis=0)**2+
                                         epochs_std.std(axis=0)**2)
                epochs_mean = epochs_data.mean(axis=0)
                values_dict[value] = (epochs_mean,epochs_std)
            else:
                values_dict[value] = epochs_data
        if band is not None:
            frequencies = [f for f in frequencies if f > band[1] and f < band[2]]
        return values_dict,frequencies

    def _get_data(self,epochs,values,value_indices,tmin,tmax,mean_and_std=True):
        if type(tmin) is dict:
            tmin = min(tmin.values())
        if type(tmax) is dict:
            tmax = max(tmax.values())
        epochs = epochs.copy().crop(tmin=tmin,tmax=tmax)
        epochs_data = epochs.get_data()
        if mean_and_std:
            epochs_std = epochs_data.std(axis=0)
            epochs_mean = epochs_data.mean(axis=0)
            values_dict = {'all':(epochs_mean,epochs_std)}
        else:
            values_dict = {'all':epochs_data}
        for value in values:
            indices = value_indices[value]
            if mean_and_std:
                epochs_std = epochs_data[indices].std(axis=0)
                epochs_mean = epochs_data[indices].mean(axis=0)
                values_dict[value] = (epochs_mean,epochs_std)
            else:
                values_dict[value] = epochs_data[indices]
        return values_dict

    def _get_times(self,epochs,event,buffered=False,tmin=None,tmax=None):
        if tmin is None:
            _,tmin,_ = self.events[event]
        if tmax is None:
            _,_,tmax = self.events[event]
        if buffered:
            tmin -= self.tbuffer
            tmax += self.tbuffer
        if type(tmin) is dict:
            tmin = min(tmin.values())
        if type(tmax) is dict:
            tmax = max(tmax.values())
        times = epochs.times
        tind = np.intersect1d(np.where(tmin<=times),np.where(times<=tmax))
        return times[tind]

    def getEventTimes(self,event):
        '''do this on the events from the raw since we don't want to have any
        unassigned events for dropped epochs in case dropped epochs need
        a designation for whatever reason'''
        raw = self._load_raw()
        stim_ch,_,_ = self.events[event]
        events = find_events(raw,stim_ch,output='onset')
        return raw.times[events[:,0]]

    def _add_last_square_legend(self,fig,*labels):
        ax = fig.add_axes([0.92, 0.1, 0.05, 0.8])
        ax.axis('off')
        for label in labels:
            ax.plot(0,0,label=label)
        ax.legend(loc='center')

    def _plotter_main(self,event,condition,values,ar=False,keyword=None,
                      aux=False,butterfly=False,contrast=False,
                      tfr=False,band=None,tfr_keyword=None,tmin=None,tmax=None,
                      vmin=None,vmax=None,show=True):
        heatmap = tfr and band is None
        epochs = self._load_epochs(event,ar=ar,keyword=keyword)
        values = self._default_values(values,condition,contrast)
        value_indices = self._get_indices(epochs,condition,values)
        ch_dict = self._get_ch_dict(epochs,aux=aux)
        fig,axs = self._setup_plot(ch_dict,butterfly=butterfly,values=values)
        tmin,tmax = self._default_t(event,tmin,tmax)
        times = self._get_times(epochs,event,tmin=tmin,tmax=tmax)
        if tfr:
            tind = np.array([i for i,t in enumerate(times) if
                             t >= tmin and t<=tmax])
            values_dict,frequencies = \
                self._get_tfr_data(event,condition,values,tfr_keyword,
                                   value_indices,tind,band=band)
        else:
            values_dict = self._get_data(epochs,values,value_indices,tmin,tmax)
            frequencies = None
        if contrast:
            epochs_mean0,epochs_std0 = values_dict[values[0]]
            epochs_mean1,epochs_std1 = values_dict[values[1]]
            epochs_std = np.sqrt(epochs_std0**2 + epochs_std1**2)
            epochs_mean = epochs_mean1-epochs_mean0
            self._plot_decider(epochs_mean,epochs_std,times,axs,fig,butterfly,
                               contrast,values,ch_dict,tfr,band,frequencies,
                               vmin,vmax)
        else:
            for i,value in enumerate(values):
                epochs_mean,epochs_std = values_dict[value]
                if butterfly:
                    axs[i].set_title(value)
                    self._plot_decider(epochs_mean,epochs_std,times,axs[i],fig,
                                       butterfly,contrast,values,ch_dict,tfr,
                                       band,frequencies,vmin,vmax)
                else:
                    self._plot_decider(epochs_mean,epochs_std,times,axs,fig,
                                       butterfly,contrast,values,ch_dict,tfr,
                                       band,frequencies,vmin,vmax)
        if not (heatmap or butterfly):
            if contrast:
                self._add_last_square_legend(fig,'%s-%s' %(values[0],values[1]))
            else:
                self._add_last_square_legend(fig,*values)

        self._prepare_fig(fig,event,condition,values,aux=aux,butterfly=butterfly,
                          contrast=contrast,tfr=tfr,band=band,ar=ar,
                          keyword=keyword,show=show)

    def _plot_decider(self,epochs_mean,epochs_std,times,axs,fig,butterfly,
                      contrast,values,ch_dict,tfr,band,frequencies,vmin,vmax,
                      clusters=None,cluster_p_values=None):
        vmin,vmax = self._default_vs(epochs_mean[ch_dict.keys()],
                                     epochs_std[ch_dict.keys()],vmin,vmax)
        if tfr:
            if band is not None:
                self._plot_band(epochs_mean,epochs_std,times,axs,ch_dict,
                                butterfly,vmin,vmax,clusters=clusters,
                                cluster_p_values=cluster_p_values)
            else:
                self._plot_heatmap(epochs_mean,epochs_std,times,axs,fig,
                                   butterfly,ch_dict,frequencies,vmin,vmax,
                                   clusters=clusters,
                                   cluster_p_values=cluster_p_values)
        else:
            self._plot_voltage(epochs_mean,epochs_std,times,axs,butterfly,
                               ch_dict,vmin,vmax,clusters=clusters,
                               cluster_p_values=cluster_p_values)

    def _plot_voltage(self,epochs_mean,epochs_std,times,axs,butterfly,ch_dict,
                      vmin,vmax,clusters=None,cluster_p_values=None):
        epochs_mean *= 1e6
        epochs_std *= 1e6
        vmin *= 1e6
        vmax *= 1e6
        for i,ch in enumerate(ch_dict):
            if butterfly:
                ax = axs
            else:
                ax = axs[i]
                ax.set_title(ch_dict[ch])
            ax.axvline(0,color='k')
            ax.set_ylim(vmin,vmax)
            v = epochs_mean[ch]-epochs_mean[ch].mean()
            lines = ax.plot(times,v,color='k')
            if not butterfly:
                ax.fill_between(times,v-epochs_std[ch],v+epochs_std[ch],
                                color=lines[0].get_color(),alpha=0.5)
            if clusters and cluster_p_values:
                for i_c, c in enumerate(clusters):
                    c = c[0]
                    if cluster_p_values[i_c] <= 0.05:
                        h = ax.axvspan(time[c.start],time[c.stop - 1],color='r',
                                       alpha=0.3)
                    else:
                        ax.axvspan(time[c.start],time[c.stop-1],
                                   color=(0.3, 0.3, 0.3),alpha=0.3)

    def _plot_heatmap(self,epochs_mean,epochs_std,times,axs,fig,butterfly,
                      ch_dict,frequencies,vmin,vmax,clusters=None,
                      cluster_p_values=None):
        cmap = plt.get_cmap('cool')
        norm = SymLogNorm(vmax/10,vmin=vmin,vmax=vmax)
        tmin,tmax = times.min(),times.max()
        fmin,fmax = frequencies.min(),frequencies.max()
        extent=[tmin,tmax,fmin,fmax]
        aspect=1.0/(fmax-fmin)
        for i,ch in enumerate(ch_dict):
            if butterfly:
                ax = axs
            else:
                ax = axs[i]
                ax.set_title(ch_dict[ch])
            if clusters:
                current_data = np.zeros(clusters[0].shape)
                for cluster,p in zip(clusters,cluster_p_values):
                    if p < 0.05:
                        current_data += cluster[::-1]*vmax
                    else:
                        current_data += cluster[::-1]*vmax*0.5
                image = np.ones((current_data.shape[0],
                                 current_data.shape[1],3))*vmax
                image[:,:,0] = current_datae
                image[:,:,1:2] = 0
                ax.imshow(current_data,aspect=aspect,norm=norm,extent=extent)
            else:
                current_data = cmap(norm(epochs_mean[ch,::-1]))
                im = ax.imshow(current_data,aspect=aspect,extent=extent,
                               cmap=cmap,norm=norm)
            ax.set_xticks(np.round(np.linspace(tmin,tmax,5),2))
            frequency_labels = np.round(frequencies[::10],2)
            ax.set_yticks(np.round(np.linspace(fmin,fmax,5),2))
        cbar_ax = fig.add_axes([0.92, 0.1, 0.05, 0.8])
        fig.colorbar(im, cax=cbar_ax)

    def _plot_band(self,epochs_mean,epochs_std,times,axs,ch_dict,butterfly,
                   vmin,vmax,clusters=None,cluster_p_values=None):
        for i,ch in enumerate(ch_dict):
            if butterfly:
                ax = axs
            else:
                ax = axs[i]
                ax.set_title(ch_dict[ch])
            lines = ax.plot(times,epochs_mean[ch])
            if not butterfly:
                ax.fill_between(times,epochs_mean[ch]-epochs_std[ch],
                                epochs_mean[ch]+epochs_std[ch],
                                color=lines[0].get_color(),alpha=0.5)
            ax.axvline(0,color='k')
            ax.set_ylim(vmin,vmax)
            if clusters and cluster_p_values:
                for i_c, c in enumerate(clusters):
                    c = c[0]
                    if cluster_p_values[i_c] <= 0.05:
                        h = ax.axvspan(times[c.start],times[c.stop-1],color='r',
                                       alpha=0.3)
                    else:
                        ax.axvspan(timse[c.start],times[c.stop-1],
                                   color=(0.3, 0.3, 0.3),alpha=0.3)

    def _prepare_fig(self,fig,event,condition,values,aux=False,
                     butterfly=False,contrast=False,tfr=False,band=None,
                     ar=False,keyword=None,show=True):
        if tfr:
            if band:
                ylabel = 'Relative Abundance'
            else:
                ylabel = 'Frequency (Hz)'
        else:
            ylabel = r"$\mu$V"
        fig.text(0.02, 0.5, ylabel, va='center', rotation='vertical')
        fig.text(0.5, 0.02, 'Time (s)', ha='center')
        fig.set_size_inches(20,15)
        title = (event + ' ' + condition + ' ' +
                 ' '.join([str(value) for value in values]) +
                 ' contrast'*contrast)
        if tfr and band:
            bandname,_,_ = band
            title += (' ' + bandname + ' band')
        else:
            bandname = ''
        fig.suptitle(title)
        fig.savefig(self._fname('plots','plot','.jpg',contrast*'contrast',
                                'tfr'*tfr,'aux'*aux,'butterfly'*butterfly,
                                (bandname + '_band')*(band is not None),
                                'ar'*ar,keyword,event,condition,*values))
        self._show_fig(fig,show)

    def makeWavelets(self,event,condition,values=None,ar=False,keyword_in=None,
                     keyword_out=None,fmin=3,fmax=35,nmin=3,nmax=10,steps=32,
                     compressed=False,normalize=True,overwrite=False):
        #note compression may not always work
        values = self._default_values(values,condition,contrast=False)
        frequencies = np.logspace(np.log10(fmin),np.log10(fmax),steps)
        n_cycles = np.logspace(np.log10(nmin),np.log10(nmax),steps)
        epochs = self._load_epochs(event,ar=ar,keyword=keyword_in)
        _,tmin,tmax = self.events[event]
        times = self._get_times(epochs,event)
        value_indices = self._get_indices(epochs,condition,values)
        values_dict = self._get_data(epochs,values,value_indices,tmin-self.tbuffer,
                                     tmax+self.tbuffer,mean_and_std=False)
        if normalize:
            bl_epochs = self._load_epochs('Baseline',ar=ar,keyword=keyword_in)
            _,bl_tmin,bl_tmax = self.events['Baseline']
            bl_times = self._get_times(bl_epochs,'Baseline')
            bl_value_indices = self._get_indices(bl_epochs,condition,values)
            bl_values_dict = self._get_data(bl_epochs,values,bl_value_indices,
                                            bl_tmin-self.tbuffer,
                                            bl_tmax+self.tbuffer,mean_and_std=False)
        for value in values:
            if (overwrite or not
                (os.path.isfile(self._fname('TFR','tfr','.npz',
                                            'Baseline',condition,value)) and
                 os.path.isfile(self._fname('TFR','tfr','.npz',
                                            event,condition,value)))):
                if normalize:
                    bl_tfr = tfr_array_morlet(bl_values_dict[value],
                                              sfreq=bl_epochs.info['sfreq'],
                                              freqs=frequencies,n_cycles=n_cycles,
                                              output='power')
                    bl_tind = bl_epochs.time_as_index(bl_times) #crop buffer
                    bl_tfr = bl_tfr[:,:,:,bl_tind]
                    self._save_TFR(bl_tfr,frequencies,n_cycles,'Baseline',condition,
                                   value,keyword_out,compressed=compressed)
                    bl_power = bl_tfr.mean(axis=0).mean(axis=-1) #average over epochs,times
                    bl_power = bl_power[np.newaxis,:,:,np.newaxis]
                current_data = values_dict[value]
                current_data -= current_data.mean(axis=0)
                tfr = tfr_array_morlet(current_data,sfreq=epochs.info['sfreq'],
                                       freqs=frequencies,n_cycles=n_cycles,
                                       output='power')
                tind = epochs.time_as_index(times) #crop buffer
                tfr = tfr[:,:,:,tind]
                if normalize:
                    tile_shape = (current_data.shape[0],1,1,len(times))
                    bl_power = np.tile(bl_power,tile_shape)
                    tfr /= bl_power #normalize
                self._save_TFR(tfr,frequencies,n_cycles,event,condition,value,
                               keyword_out,compressed=compressed)
                del tfr
                del bl_tfr
            else:
                print('TFR already calculated for %s,' %(value) +
                      ' use keyword overwrite=True to recalculate')

    def psdMultitaper(self,preprocessed=False,ica=False,keyword=None,ch='Oz',
                      N=6,deltaN=0.25,NW=3.0,fmin=0.5,fmax=25,BW=1.0,
                      assign_states=True,labels={'Wake':'red','Sleep':'white'},
                      overwrite=False,n_jobs=10,vmin=None,vmax=None,
                      adaptive=False,jackknife=True,low_bias=True):
        # full-night: N = 30.0 s, deltaN = 5.0 s, deltaf = 1.0 Hz, TW = 15, L = 29
        # ultradian: N = 6.0 s, deltaN = 0.25 s, deltaf = 1.0 Hz, TW = 3, L = 5
        # microevent: N = 2.5 s, deltaN = 0.05 s, deltaf = 4.0 Hz, TW = 5, L = 9
        #ch = 'EEG072'
        raw = self._load_raw(preprocessed=preprocessed,ica=ica,keyword=keyword)

        ch_ind = raw.ch_names.index(ch)
        raw_data = raw.get_data(picks=ch_ind).flatten()
        Fs = raw.info['sfreq']

        n_full_windows = int(np.floor(raw.times[-1]/N))
        t_end = raw.times[int(n_full_windows*N*Fs)]
        n_windows = int((n_full_windows-1) * (N/deltaN)) + 1

        if overwrite:
            image = None
        else:
            image = self._load_PSD_image(preprocessed,ica,keyword,ch,
                                         N,deltaN,fmin,fmax,NW)

        if image is None:
            imsize = int(Fs/2*N) + 1
            image = np.zeros((imsize,int(n_full_windows*(N/deltaN))))
            counters = np.zeros((int(n_full_windows*(N/deltaN))))
            with Parallel(n_jobs=n_jobs) as parallel:
                results = parallel(delayed(tsa.multi_taper_psd)(
                            raw_data[int(round(i*deltaN*Fs)):
                                     int(round((i*deltaN+N)*Fs))],
                            Fs=Fs,NW=NW,BW=BW,adaptive=adaptive,
                            jackknife=jackknife,low_bias=low_bias)
                                    for i in tqdm(range(n_windows)))
            fs, psd_mts, nus = zip(*results)
            for i in range(n_windows):
                for j in range(i,i+int(N/deltaN)):
                    image[:,j] += np.log10(psd_mts[i])
                    counters[j] += 1
            for k in range(imsize):
                image[k] /= counters
            f = np.linspace(0,Fs/2,imsize)
            f_inds = [i for i,freq in enumerate(f) if
                      (freq >= fmin and freq <= fmax)]
            image = image[f_inds]
            self._save_PSD_image(image,preprocessed,ica,keyword,ch,
                                 N,deltaN,fmin,fmax,NW)

        fig2, ax2 = plt.subplots()
        fig2.set_size_inches(12,8)
        fig2.subplots_adjust(right=0.8)
        im2 = ax2.imshow(image,aspect='auto',cmap='jet',vmin=vmin,vmax=vmax)

        cax2 = fig2.add_axes([0.82, 0.1, 0.05, 0.8])
        fig2.colorbar(im2,cax=cax2)

        fig2.suptitle('Multitaper Spectrogram')

        if assign_states:
            drs = {label:[] for label in labels}
            fig,ax1 = plt.subplots()
            fig.suptitle('Multitaper Spectrogram')
            fig.set_size_inches(12,8)
            fig.subplots_adjust(right=0.7)
            buttons = []
            button_height = 0.8/(len(labels)+1)
            y0 = 0.1 + button_height/(len(labels)+1)
            for label in labels:
                label_ax = fig.add_axes([0.85, y0, 0.1, button_height])
                y0 += button_height + button_height/(len(labels)+1)
                buttons.append(ButtonClickProcessor(label_ax,label,labels[label],
                                                    ax1,drs,image))
            im = ax1.imshow(image,aspect='auto',cmap='jet',vmin=vmin,vmax=vmax)
            cax = fig.add_axes([0.72, 0.1, 0.05, 0.8])
            fig.colorbar(im,cax=cax)
            axs = [ax1,ax2]
        else:
            axs = [ax2]

        for ax in axs:
            ax.invert_yaxis()
            ax.set_yticks(np.linspace(0,image.shape[0],10))
            ax.set_yticklabels(np.round(np.linspace(fmin,fmax,10),1))
            ax.set_ylabel('Frequency (Hz)')
            ax.set_xticks(np.linspace(0,image.shape[1],10))
            ax.set_xticklabels(np.round(np.linspace(0,t_end,10)))
            ax.set_xlabel('Time (s)')
        fig2.savefig(self._fname('plots','psd_multitaper','.jpg',
                                 'preprocessed'*preprocessed,'ica'*ica,
                                 'N_%i_dN_%.2f' %(N,deltaN),
                                 'fmin_%.2f_fmax_%.2f_NW_%i' %(fmin,fmax,NW)))
        plt.close(fig2)

        if assign_states:
            plt.show(fig)
            state_times = {label:[] for label in labels}
            for label in drs:
                for dr in drs[label]:
                    rect = dr.rect
                    start = rect.get_x()*deltaN
                    duration = rect.get_width()*deltaN
                    state_times[label].append((start,(start+duration)))
            return state_times

    def assignConditionFromStateTimes(self,event,condition,state_times,
                                      no_state='Neither'):
        event_times = self.getEventTimes(event)
        states = np.tile(no_state,len(event_times))
        for i,t in enumerate(event_times):
            for state in state_times:
                if any([t <= tmax and t >= tmin for
                        tmin,tmax in state_times[state]]):
                    states[i] = state
        self.behavior[condition] = states
        self._save_behavior()

    def CPTByBand(self,event,condition,values=[],ar=False,keyword=None,
                  tfr_keyword=None,tmin=None,tmax=None,threshold=6.0,aux=False,
                  bands={'theta':(4,8),'alpha':(8,15),'beta':(15,30)},
                  contrast=True):
        for band in bands:
            fmin,fmax = bands[band]
            band_struct = (band,fmin,fmax)
            self.CPT(event,condition,values,ar=ar,keyword=keyword,
                     tfr_keyword=tfr_keyword,tmin=tmin,tmax=tmax,tfr=True,
                     threshold=threshold,band=band_struct,contrast=contrast,
                     aux=aux)

    def sourceCPT(self,event,condition,values=None,ar=False,keyword=None,
                  tmin=None,tmax=None,threshold=4.0,
                  contrast=False,aux=False,n_permutations=1000,n_jobs=10):
        alpha = 0.3
        values = self._default_values(values,condition,contrast)
        tmin,tmax = self._default_t(event,tmin,tmax)
        if contrast:
            source_data0 = self._load_source(event,condition,values[0],ar=ar,keyword=keyword)
            source_data1 = self._load_source(event,condition,values[1],ar=ar,keyword=keyword)
            clusters,cluster_p_values = \
                    self._CPT(source_data0,source_data1,threshold,ch_dict,
                              n_permutations=n_permutations,n_jobs=n_jobs)
            self._save_CPT(event,condition,'%s-%s' %(values[0],values[1]),
                           clusters,cluster_p_values,times,
                           frequencies=frequencies,band=band)
        else:
            for value in values:
                source_data = self._load_source(event,condition,value,ar=ar,keyword=keyword)
                bl_source_data,_ = self._load_source(event,condition,value,ar=ar,keyword=keyword)
                bl_source_data = self._equalize_baseline_length(source_data,
                                                                bl_source_data)
                clusters,cluster_p_values = \
                    self._CPT(source_data,bl_source_data,threshold,
                              n_permutations=n_permutations,n_jobs=n_jobs)
                self._save_CPT(event,condition,value,clusters,
                               cluster_p_values,times,
                               frequencies=frequencies,band=band)

    def CPT(self,event,condition,values=None,ar=False,keyword=None,
            tmin=None,tmax=None,threshold=4.0,tfr=False,band=None,tfr_keyword=None,
            contrast=False,n_permutations=1000,n_jobs=10):
        # plot cluster perumation test
        heatmap = tfr and not band
        if heatmap:
            alpha = 1
        else:
            alpha = 0.3
        values = self._default_values(values,condition,contrast)
        tmin,tmax = self._default_t(event,tmin,tmax)
        epochs = self._load_epochs(event,ar=ar,keyword=keyword)
        epochs = epochs.pick_types(meg=self.meg,eeg=self.eeg)
        times = self._get_times(epochs,event,tmin=tmin,tmax=tmax)
        value_indices = self._get_indices(epochs,condition,values)
        if not contrast:
            bl_epochs = self._load_epochs('Baseline',ar=ar,keyword=keyword)
            bl_value_indices = self._get_indices(bl_epochs,condition,values)
        ch_dict = self._get_ch_dict(epochs,aux=aux)
        if tfr:
            tind = np.array([i for i,t in enumerate(times) if
                             t >= tmin and t<=tmax])
            values_dict,frequencies = \
                self._get_tfr_data(event,condition,values,tfr_keyword,value_indices,
                                   tind,band=band,mean_and_std=False)
            if not contrast:
                bl_values_dict,bl_frequencies = \
                    self._get_tfr_data('Baseline',condition,values,tfr_keyword,
                                       bl_value_indices,t_ind,band=band)
        else:
            values_dict = self._get_data(epochs,values,value_indices,tmin,tmax)
            if not contrast:
                bl_values_dict = self._get_data(bl_epochs,values,bl_value_indices,
                                                tmin,tmax)
            frequencies = None
        if contrast:
            epochs_data0 = values_dict[values[0]]
            epochs_data1 = values_dict[values[1]]
            clusters,cluster_p_values = \
                    self._CPT(epochs_data0,epochs_data1,threshold,
                              n_permutations=n_permutations,n_jobs=n_jobs)
            self._save_CPT(event,condition,'%s-%s' %(values[0],values[1]),
                           clusters,cluster_p_values,times,
                           frequencies=frequencies,band=band)
        else:
            for value in values:
                evo_data,_ = values_dict[value]
                bl_evo_data,_ = bl_values_dict[value]
                bl_evo_data = self._equalize_baseline_length(evo_data,bl_evo_data)
                clusters,cluster_p_values = \
                    self._CPT(evo_data,bl_evo_data,threshold,
                              n_permutations=n_permutations,n_jobs=n_jobs)
                self._save_CPT(event,condition,value,clusters,
                               cluster_p_values,times,
                               frequencies=frequencies,band=band)

    def _equalize_baseline_length(value_data,bl_data):
        bl_len = bl_data.shape[-1]
        val_len = value_data.shape[-1]
        if bl_len < val_len:
            n_reps = val_len/bl_len
            remainder = val_len% bl_len
            print('Using %.2f ' %(n_reps + float(remainder)/bl_len) +
                  'repetitions of the baseline period for permuation')
            baseline = np.tile(bl_data,n_reps)
            remainder_baseline = np.take(bl_data,
                                         range(bl_len-remainder,bl_len),
                                         axis=-1)
                              #take from the end of the baseline period
            bl_data = np.concatenate((baseline,remainder_baseline),axis=-1)
        elif bl_len > ep_len:
            bl_data = np.take(bl_data,range(val_len),axis=-1)
        return bl_data

    def _CPT(self,data0,data1,threshold,n_permutations=1000,n_jobs=10):
        T_obs, clusters, cluster_p_values, H0 = \
        permutation_cluster_test([data0,data1],n_permutations=n_permutations,
                                  threshold=threshold,tail=0,n_jobs=n_jobs,
                                  buffer_size=None,verbose=False)
        return clusters,cluster_p_values

    def plotControlVariables(self,conditions=None):
        if conditions is None:
            conditions = list(self.behavior.keys())
        for param in conditions:
            fig,(ax1,ax2) = plt.subplots(1,2)
            if any([isinstance(self.behavior[param][index],str)
                    for index in range(self.n)]):
                sns.countplot(self.behavior[param],ax=ax1)
                sns.swarmplot(x=range(self.n),y=self.behavior[param],ax=ax2)
            else:
                var = [v for v in self.behavior[param] if not np.isnan(v)]
                t = [i for i,v in enumerate(self.behavior[param]) if not
                        np.isnan(v)]
                sns.distplot(var,bins=10,ax=ax1)
                ax2 = sns.pointplot(x=t,y=var,join=False,ax=ax2)
            ax1.set_title('Histogram')
            ax1.set_xlabel(param)

            ax2.set_title('Time Course')
            ax2.set_xlabel('Time')
            ax2.set_ylabel(param)
            fig.show()

        params = conditions
        for i in range(len(conditions)):
            for j in range(i+1,len(conditions)):
                fig,ax = plt.subplots()
                iscat1 = (any([isinstance(self.behavior[params[i]][index],str)
                              for index in range(self.n)]) or
                          len(np.unique(self.behavior[params[i]]))>5)
                iscat2 = (any([isinstance(self.behavior[params[j]][index],str)
                              for index in range(self.n)]) or
                          len(np.unique(self.behavior[params[j]]))>5)
                if iscat1 and iscat2:
                    sns.countplot(x=self.behavior[params[i]],
                                  hue=self.behavior[params[j]],ax=ax)
                elif iscat1 or iscat2:
                    sns.stripplot(x=self.behavior[params[i]],
                                  y=self.behavior[params[j]],jitter=True)
                    sns.violinplot(x=self.behavior[params[i]],
                                  y=self.behavior[params[j]])
                else:
                    indices = [index for index in range(self.n) if not
                               (np.isnan(self.behavior[params[i]][index]) or
                                np.isnan(self.behavior[params[j]][index]))]
                    column1 = [self.behavior[params[i]][index]
                               for index in indices]
                    column2 = [self.behavior[params[j]][index]
                               for index in indices]
                    heatmat = np.histogram2d(column1,column2,bins=9)
                    centers1 = (heatmat[1][:-1] +
                                float(heatmat[1][1]-heatmat[1][0])/2)
                    centers1 = [round(c,2) for c in centers1]
                    centers2 = (heatmat[2][:-1] +
                                float(heatmat[2][1]-heatmat[2][0])/2)
                    centers2 = [round(c,2) for c in centers2]
                    heatdf = DataFrame(columns=centers1,index=centers2,
                                       data=heatmat[0])
                    sns.heatmap(heatdf,ax=ax)
                ax.set_title('%s %s distribution' %(params[i],params[j]))
                ax.set_xlabel(params[i])
                ax.set_ylabel(params[j])
                fig.show()

    def markAutoReject(self,event,keyword=None,bad_ar_threshold=0.5,n_jobs=10,
                       n_interpolates=[1,2,3,5,7,10,20],random_state=89,
                       consensus_percs=np.linspace(0,1.0,11),overwrite=False):
        if (os.path.isfile(self._fname('epochs','epo','.fif',event,'ar')) and
            not overwrite):
           print('Autoreject already calculated, use \'overwrite=True\' to '+
                 'recalculate.')
           return
        epochs = self._load_epochs(event,keyword=keyword)
        picks = pick_types(epochs.info,meg=self.meg,eeg=self.eeg,stim=False,
                           eog=False,exclude=epochs.info['bads'])
        ar = AutoReject(n_interpolates,consensus_percs,
                        picks=picks,random_state=random_state,
                        n_jobs=n_jobs,verbose='tqdm')
        epochs_ar, reject_log = ar.fit_transform(epochs,return_log=True)
        rejected = float(sum(reject_log.bad_epochs))/len(epochs)
        print('\n\n\n\n\n\nAutoreject rejected %.0f%% of epochs\n\n\n\n\n\n'%(100*rejected))
        self._save_epochs(epochs_ar,event,ar=True)
        self._save_autoreject(event,ar,reject_log)

    def plotAutoReject(self,event,keyword=None,ylim=dict(eeg=(-15, 15)),show=True):
        epochs_ar = self._load_epochs(event,ar=True)
        epochs_comparison = self._load_epochs(event,keyword=keyword)
        ar,reject_log = self._load_autoreject(event)

        set_matplotlib_defaults(plt, style='seaborn-white')
        if self.eeg:
            losses = {'eeg':ar.loss_['eeg'].mean(axis=-1)}  # losses are stored by channel type.
        else:
            losses = {'grad':ar.loss_['grad'].mean(axis=-1),
                    'mag':ar.loss_['mag'].mean(axis=-1)}
                      # losses are stored by channel type.
        fig = epochs_ar.plot_drop_log(show=False)
        self._show_fig(fig,show)

        for modality in losses:
            loss = losses[modality]
            fig,ax = plt.subplots()
            im = ax.matshow(loss.T * 1e6, cmap=plt.get_cmap('viridis'))
            ax.set_xticks(range(len(ar.consensus)))
            ax.set_xticklabels(ar.consensus)
            ax.set_yticks(range(len(ar.n_interpolate)))
            ax.set_yticklabels(ar.n_interpolate)

            # Draw rectangle at location of best parameters
            idx, jdx = np.unravel_index(loss.argmin(), loss.shape)
            rect = patches.Rectangle((idx - 0.5, jdx - 0.5), 1, 1, linewidth=2,
                                     edgecolor='r', facecolor='none')
            ax.add_patch(rect)
            ax.xaxis.set_ticks_position('bottom')
            ax.set_xlabel(r'Consensus percentage $\kappa$')
            ax.set_ylabel(r'Max sensors interpolated $\rho$')
            ax.set_title('Mean cross validation error (x 1e6) %s' %(modality))
            fig.colorbar(im)
            fig.savefig(self._fname('plots','ar','.jpg',event,modality,
                                    'consensus_v_interpolates'))
            self._show_fig(fig,show)

        fig = epochs_comparison.average().plot(ylim=ylim, spatial_colors=True,
                                               window_title='Before Autoreject',
                                               show=False)
        fig.savefig(self._fname('plots','ar','.jpg',event,
                                'ar_before'))
        self._show_fig(fig,show)
        epochs_ar.average().plot(ylim=ylim,spatial_colors=True,
                                 window_title='After Autoreject',show=False)
        fig.savefig(self._fname('plots','ar','.jpg',event,
                                'ar_after'))
        self._show_fig(fig,show)

    def plotRejectLog(self,event,keyword=None,scalings=dict(eeg=40e-6)):
        ar,reject_log = self._load_autoreject(event)
        epochs_comparison = self._load_epochs(event,keyword=keyword)
        reject_log.plot_epochs(epochs_comparison,scalings=scalings,
                               title='Dropped and interpolated Epochs')

    def epochs2source(self,fs_dir,bemf,sourcef,coord_transf,
                      event,condition,values=None,snr=1.0,ar=False,
                      keyword_in=None,keyword_out=None,method='dSPM',
                      pick_ori='normal',shared_baseline=False,overwrite=False):
        keyword_out = keyword_in if keyword_out is None else keyword_out
        values = self._default_values(values,condition)
        epochs = self._load_epochs(event,ar=ar,keyword=keyword_in)
        bl_epochs = self._load_epochs('Baseline',ar=ar,keyword=keyword_in)
        value_indices = self._get_indices(epochs,condition,values)

        if (all([os.path.isfile(self._fname('sources','source-lh','.stc',event,
                                            condition,value))
                 for value in values]) and not overwrite):
            print('Sources already computed, use \'overwrite=True\' ' +
                  'to recalculate')
            return

        bem,source,coord_trans,lambda2,epochs,bl_epochs,fwd = \
            self._source_setup(fs_dir,bemf,sourcef,coord_transf,event,snr,
                               epochs,bl_epochs)

        if shared_baseline:
            print('Making inverse for all...')
            inv = self._generate_inverse(epochs,fwd,bl_epochs,lambda2,method,
                                         pick_ori)
            self._save_inverse(inv,lambda2,method,pick_ori,
                               event,condition,'all',ar,keyword_out)
            print('Applying inverse on baseline for all...')
            bl_stc = apply_inverse(bl_evoked,inv,lambda2=lambda2,method=method,
                                   pick_ori=pick_ori)
            self._save_source(bl_stc,'Baseline',condition,'all',ar,keyword_out)
        else:
            bl_value_indices = self._get_indices(bl_epochs,condition,values)
        for value in values:
            if not shared_baseline:
                if not value in bl_value_indices: #if the baseline was corrupted,
                    continue                      #don't use the trial
                bl_indices = bl_value_indices[value]
                print('Making inverse for %s...' %(value))
                inv = self._generate_inverse(epochs,fwd,bl_epochs[bl_indices],
                                             lambda2,method,pick_ori)
                self._save_inverse(inv,lambda2,method,pick_ori,
                                   event,condition,value,ar,keyword_out)
                print('Applying inverse on baseline for %s...' %(value))
                bl_evoked = bl_epochs[bl_indices].average()
                bl_stc = apply_inverse(bl_evoked,inv,lambda2=lambda2,
                                       method=method,pick_ori=pick_ori)
                self._save_source(bl_stc,'Baseline',condition,value,ar,
                                  keyword_out)
            print('Applying inverse for %s' %(value))
            indices = value_indices[value]
            current_epochs = epochs[indices]
            evoked = current_epochs.average()
            stc = apply_inverse(evoked,inv,lambda2=lambda2,method=method,
                                pick_ori=pick_ori)
            self._save_source(stc,event,condition,value,ar,keyword_out)

    def _source_setup(self,fs_dir,bemf,sourcef,coord_transf,event,snr,
                      epochs,bl_epochs):
        set_config("SUBJECTS_DIR",fs_dir,set_env=True)

        bem = read_bem_solution(os.path.join(fs_dir,bemf))
        source = read_source_spaces(os.path.join(fs_dir,sourcef))
        coord_trans = read_trans(os.path.join(fs_dir,coord_transf))

        # Source localization parameters.
        lambda2 = 1.0 / snr ** 2

        _,tmin,tmax = self.events[event]
        epochs = epochs.crop(tmin=tmin,tmax=tmax)
        if self.eeg:
            epochs = epochs.set_eeg_reference(ref_channels='average',
                                              projection=True)

        bads = list(np.unique(epochs.info['bads'] + bl_epochs.info['bads']))
        epochs.info['bads'] = bads
        bl_epochs.info['bads'] = bads

        _,bl_tmin,bl_tmax = self.events['Baseline']
        bl_epochs = bl_epochs.crop(tmin=bl_tmin,tmax=bl_tmax)
        if self.eeg:
            bl_epochs = bl_epochs.set_eeg_reference(ref_channels='average',
                                                    projection=True)
        print('Making forward model...')
        fwd = make_forward_solution(epochs.info,coord_trans,source,bem,
                                    meg=self.meg,eeg=self.eeg,mindist=1.0)
        return bem,source,coord_trans,lambda2,epochs,bl_epochs,fwd

    def _generate_inverse(self,epochs,fwd,bl_epochs,lambda2,method,pick_ori):
        noise_cov = compute_covariance(bl_epochs,method="shrunk")
        inv = make_inverse_operator(epochs.info, fwd, noise_cov)
        return inv

    def fsaverageMorph(self,event,condition,values=None,ar=False,keyword=None):
        values = self._default_values(values,condition)
        for value in values:
            stc = self._load_source(event,condition,value,ar=ar,keyword=keyword)
            stc_fs = stc.morph('fsaverage')
            self._save_source(stc_fs,event,condition,value,fs_av=True,ar=ar,
                              keyword=keyword)

    def sourceContrast(self,event,condition,values=None,ar=False,keyword=None):
        if len(values) != 2:
            raise ValueError('Can only contrast two values at once')
        else:
            stc0 = self._load_source(event,condition,values[0],ar=True)
            stc1 = self._load_source(event,condition,values[1],ar=True)
            if stc0 and stc1:
                stc_con = stc0-stc1
                self._save_source(stc_con,event,condition,
                                  '%s %s Contrast' %(values[0],values[1]),
                                  ar=ar,keyword=keyword)

    def plotSourceSpace(self,event,condition,values=None,tmin=None,tmax=None,
                        fs_av=False,ar=False,keyword=None,downsample=False,
                        seed=11,hemi='both',size=(800,800),time_dilation=10,
                        clim='default',use_saved_stc=False, gif_combine=True,
                        views=['lat','med','cau','dor','ven','fro','par'],
                        show=True):
        ''' This may take some time... plots each individual view and then
            combines them all in one animated gif is gif_combine=True. It's okay
            to have other windows over the mlab plots but don't minimize them
            otherwise the image write out will break! '''
        values = self._default_values(values,condition)
        tmin,tmax = self._default_t(event,tmin,tmax)
        epochs = self._load_epochs(event,ar=ar,keyword=keyword)
        epochs = epochs.crop(tmin=tmin,tmax=tmax)
        epochs = epochs.pick_types(meg=self.meg,eeg=self.eeg)
        if self.eeg:
            epochs = epochs.set_eeg_reference(ref_channels='average',
                                                     projection=True,
                                                     verbose=False)
        value_indices = self._get_indices(epochs,condition,values)
        nTR = min([len(value_indices[value]) for value in value_indices])
        if downsample:
            np.random.seed(seed)
        for value in values:
            if use_saved_stc:
                if not os.path.isfile(self._fname('sources','source-lh','.stc',
                                                  event,condition,value,
                                                  'fs_av'*fs_av)):
                    raise ValueError('The data must be converted to' +
                                     ' source space first.')
                stc = self._load_source(event,condition,value,fs_av,ar=ar,
                                        keyword=keyword)
                stc.crop(tmin=tmin,tmax=tmax)
            else:
                indices = value_indices[value]
                if downsample:
                    print('Subsampling %i/%i ' %(nTR,len(indices)) +
                          'for %s %s.' %(condition,value))
                    np.random.shuffle(indices)
                    indices = sorted(indices[:nTR])
                inv,lambda2,method,pick_ori = \
                    self._load_inverse(event,condition,value,ar=ar,keyword=keyword)
                stc_Evoked = epochs[indices].average()
                stc = apply_inverse(stc_Evoked,inv,lambda2=lambda2,
                                    method=method,pick_ori=pick_ori,verbose=False)
            if clim == 'default':
                clim = dict(kind='value',lims=(stc.data.min(),stc.data.mean(),stc.data.max()))

            gif_names = []
            for view in views:
                gif_name = self._fname('plots','source_plot','.gif',event,
                                       condition,value,'ar'*ar,keyword,hemi,view)
                fig = [mlab.figure(size=size)]
                fig = stc.plot(subjects_dir=self.subjects_dir,subject=self.subject,
                           hemi=hemi,clim=clim,views=[view],figure=fig)
                fig.save_movie(gif_name,tmin=tmin,tmax=tmax,
                               time_dilation=time_dilation)
                fig.close()
                gif_names.append(gif_name)

            if gif_combine:
                print('Combining gifs for %s' %(value))
                anim = combine_gifs(self._fname('plots','source_plot','.gif',
                                                event,condition,value,'ar'*ar,
                                                keyword,hemi,*views),
                                    60,*gif_names)
                if show:
                    plt.show()

    def interpolateArtifact(self,event,ar=False,keyword=None,
                            preprocessed=False,ica=False,mode='spline',
                            npoint_art=1,offset=0,points=10,k=3):
        #note: points and k not used in mne's interpolation (linear and window)
        #note: linear and window methods interpolate auxillary channels via spline
        if mode not in ('spline','linear','window'):
            raise ValueError('Mode must be spline/linear/window')
        stim_ch,_,_ = self.events[event]
        if preprocessed or ica:
            inst = self._load_raw(preprocessed=preprocessed,ica=ica)
            events = find_events(inst,stim_channel=stim_ch,output='onset')
        else:
            inst = self._load_epochs(event,ar=ar,keyword=keyword)
            events = None
        if mode in ('linear','window'):
            sfreq = inst.info['sfreq']
            tmin = -float(offset)/sfreq
            tmax = float(npoint_art-offset)/sfreq
            print('Interpolating using tmin %.3f tmax %.3f' %(tmin,tmax))
            interp = fix_stim_artifact(inst.copy(),events=events,tmin=tmin,
                                       tmax=tmax,mode=mode,stim_channel=stim_ch)

        inst_data = inst.copy().get_data()
        if mode == 'spline':
            ch_ind = pick_types(inst.info,meg=self.meg,eeg=self.eeg,
                                eog=True,ecg=True,stim=False)
        else:
            ch_ind = pick_types(inst.info,meg=self.meg,eeg=self.eeg,
                                eog=True,ecg=True,stim=False)
        if isinstance(inst,BaseEpochs):
            event_ind = np.where(inst.times==0)[0][0]
            interp_data = np.zeros(inst_data.shape)
            for i in range(inst_data.shape[0]):
                epoch_data = inst_data[i]
                interp_data[i] = self._interpolate(epoch_data,ch_ind,
                                                   [event_ind],npoint_art,
                                                   offset,points,k)
            interp_spline = EpochsArray(interp_data,inst.info,events=inst.events,
                                        tmin=inst.tmin,verbose=False)
        else:
            interp_data = self._interpolate(inst_data,ch_ind,events[:,0],
                                            npoint_art,offset,points,k)
            interp_spline = RawArray(interp_data,inst.info,verbose=False)
        if mode == 'spline':
            interp = interp_spline
        else:
            interp._data[ch_ind] = interp_spline._data[ch_ind]
        return interp,inst,events

    def plotInterpolateArtifact(self,event,ar=False,keyword=None,
                                preprocessed=False,ica=False,mode='linear',
                                npoint_art=1,offset=0,points=10,k=3,show=True,
                                ylim=[-5e-4,5e-4],tmin=-0.03,tmax=0.03,
                                baseline=(-1.1,-0.1)):
        if mode not in ('spline','linear','window'):
            raise ValueError('Mode must be spline/linear/window')
        interp2,inst,events = \
            self.interpolateArtifact(event,ar=ar,keyword=keyword,
                                     preprocessed=preprocessed,ica=ica,
                                     mode=mode,npoint_art=npoint_art,
                                     offset=offset,points=points,k=k)
        if preprocessed or ica:
            # Note no baseline is used but epochs are only used for visualization
            epochs = Epochs(inst,events,preload=True,verbose=False,detrend=1,
                            tmin=tmin,tmax=tmax,baseline=None)
            interp = Epochs(interp2,events,preload=True,verbose=False,detrend=1,
                            tmin=tmin,tmax=tmax,baseline=None)
        else:
            epochs = inst.copy()
            interp = interp2.copy()
            epochs = epochs.crop(tmin=tmin,tmax=tmax)
            interp = interp.crop(tmin=tmin,tmax=tmax)
        times = epochs.times
        event_ind = np.where(times==0)[0][0]
        left_edge = event_ind-offset
        right_edge = event_ind+npoint_art-offset
        base0 = range(left_edge-points,left_edge)
        base1 = range(right_edge,right_edge+points)
        interp_ind = range(left_edge,right_edge)
        other0 = range(0,left_edge-points)
        other1 = range(right_edge+points,len(times))
        this_ind = pick_types(epochs.info,meg=self.meg,eeg=self.eeg)
        aux_ind = pick_types(epochs.info,meg=False,ecg=True,eog=True)
        evoked_data = epochs.get_data().mean(axis=0)
        interp_data = interp.get_data().mean(axis=0)
        fig,(ax1,ax2) = plt.subplots(2,1)
        fig.suptitle('Evoked Colored By Interpolation Parameters')
        for ax,ch_ind in zip([ax1,ax2],[this_ind,aux_ind]):
            for ch,interp_ch in zip(evoked_data[ch_ind],interp_data[ch_ind]):
                ch_mean = ch[list(other0)+list(other1)].mean(axis=0)
                interp_ch_mean = interp_ch[list(other0)+list(other1)].mean(axis=0)
                ax.plot(times[other0],ch[other0]-ch_mean,color='k')
                ax.plot(times[other1],ch[other1]-ch_mean,color='k')
                ax.plot(times[interp_ind],ch[interp_ind]-ch_mean,color='r')
                ax.plot(times[interp_ind],interp_ch[interp_ind]-
                                          interp_ch_mean,color='g')
                ax.plot(times[base0],ch[base0]-ch_mean,color='b')
                ax.plot(times[base1],ch[base1]-ch_mean,color='b')
            ax.set_xlabel('Time (s)')
            ax.set_ylabel('V')
            ax.set_ylim(ylim)
        fig.savefig(self._fname('plots','interp','.jpg',event,'ar'*ar,keyword,
                                'preprocessed'*preprocessed,'ica'*ica,
                                'points_%i' %(points),'offset_%i' %(offset),
                                'npoint_art_%i' %(npoint_art)))
        self._show_fig(fig,show)
        return interp2,inst

    def _interpolate(self,this_data,ch_ind,events,npoint_art,offset,points,k):
        for event in events:
            left_edge = event-offset
            right_edge = event+npoint_art-offset
            x = np.concatenate((range(left_edge-points,left_edge),
                                range(right_edge,right_edge+points)))
            xnew = np.arange(left_edge,right_edge)
            y = np.concatenate((this_data[ch_ind,left_edge-points:left_edge],
                                this_data[ch_ind,right_edge:right_edge+points]),
                                axis=1)
            ynew = np.zeros((y.shape[0],xnew.shape[0]))
            for i in range(len(y)):
                tck = interpolate.splrep(x, y[i,:], s=0.0, k=k)
                ynew[i,:] = interpolate.splev(xnew, tck, der=0)
            this_data[ch_ind,left_edge:right_edge] = ynew
        return this_data

    def applyInterpolation(self,inst,event=None,keyword_out='Interpolated'):
        if isinstance(inst,BaseEpochs):
            if event is None:
                raise ValueError('The event of the epochs must be provided.')
            self._save_epochs(inst,event,keyword=keyword_out)
        elif isinstance(inst,BaseRaw):
            self._save_raw_preprocessed(inst,keyword=keyword_out)

    def filterEpochs(self,event,ar=False,keyword_in=None,keyword_out=None,
                     h_freq=None,l_freq=None):
        epochs = self._load_epochs(event,ar=ar,keyword=keyword_in)
        epochs = epochs.copy().filter(h_freq=h_freq,l_freq=l_freq)
        if keyword_out:
            ar = False
        self._save_epochs(epochs,event,ar=ar,keyword=keyword_out)

    def filterRaw(self,preprocessed=False,ica=False,keyword_in=None,
                  keyword_out=None,l_freq=None,h_freq=None,maxwell=False):
        keyword_out = keyword_out if keyword_out is not None else keyword_in
        raw = self._load_raw(preprocessed=preprocessed,ica=ica,
                             keyword=keyword_in)
        if maxwell:
            raw = maxwell_filter(raw)
        else:
            raw = raw.filter(l_freq=l_freq,h_freq=h_freq)
        if keyword_out:
            ica = False
        self._save_raw_preprocessed(raw,ica=ica,keyword=keyword_out)

    def sourceBootstrap(self,fs_dir,bemf,sourcef,coord_transf,event,snr=1.0,
                        ar=False,keyword_in=None,keyword_out=None,method='dSPM',
                        pick_ori='normal',tfr=True,itc=True,
                        fmin=7,fmax=35,nmin=3,nmax=10,steps=7,
                        bands={'alpha':(7,15),'beta':(15,35)},
                        Nboot=1000,Nave=50,seed=13,n_jobs=10,
                        use_fft=True,mode='same',batch=10,overwrite=False):
        ''' You need enough bootstraps to get a good normal distribution
            of your condition value means, 250 seems to do good. Nave is
            a tradeoff between more extreme values and lower snr of source
            estimates. Nboot is better the greater the number but 100 is
            approximately 40 GB with tfr'''
        freqs = np.logspace(np.log10(fmin),np.log10(fmax),steps)
        band_inds = {band:[i for i,f in enumerate(freqs) if
                     f >= bands[band][0] and f <= bands[band][1]]
                     for band in bands if [i for i,f in enumerate(freqs) if
                     f >= bands[band][0] and f <= bands[band][1]]}
        n_cycles = np.logspace(np.log10(nmin),np.log10(nmax),steps)
        keyword_out = keyword_in if keyword_out is None else keyword_out
        fname = self._fname('sources','bootstrap','.npz',event,
                            'ar'*(ar and not keyword_out),keyword_out)
        if os.path.isfile(fname) and not overwrite:
            raise ValueError('Bootstraps already exist, use overwrite=True')
        np.random.seed(seed)
        epochs = self._load_epochs(event,ar=ar,keyword=keyword_in)
        bl_epochs = self._load_epochs('Baseline',ar=ar,keyword=keyword_in)
        removal_indices = []
        for j,i in enumerate(epochs.events[:,2]):
            if not i in bl_epochs.events[:,2]:
                removal_indices.append(j)
        epochs = epochs.drop(removal_indices)
        bootstrap_indices = np.random.randint(0,len(epochs),(Nboot,Nave))

        bem,source,coord_trans,lambda2,epochs,bl_epochs,fwd = \
            self._source_setup(fs_dir,bemf,sourcef,coord_transf,event,snr,
                               epochs,bl_epochs)
        events = epochs.events[:,2]
        bl_events = bl_epochs.events[:,2]

        stcs = np.memmap('sb_%s_%s_%s_workfile' %(event,ar,keyword_out),
                 dtype='float64', mode='w+',
                 shape=(batch,fwd['nsource'],len(epochs.times)))
        if tfr:
            Ws = mne.time_frequency.morlet(epochs.info['sfreq'],
                                               freqs, n_cycles=n_cycles,
                                               zero_mean=False)
            powers = {band:np.memmap('sb_tfr_%s_%s_%s_workfile' %(event,ar,keyword_out),
                                     dtype='float64', mode='w+',
                                     shape=(batch,fwd['nsource'],len(epochs.times)))
                      for band in bands}
            if itc:
                itcs = {band:np.memmap('sb_tfr_%s_%s_%s_workfile' %(event,ar,keyword_out),
                                        dtype='float64', mode='w+',
                                        shape=(batch,fwd['nsource'],len(epochs.times)))
                      for band in bands}
            else:
                itcs = None
        else:
            powers = itcs = Ws = None
        mins = range(0,Nboot-batch +1,batch)
        maxs = range(batch,Nboot+1,batch)
        for i_min,i_max in zip(mins,maxs):
            print('Computing bootstraps %i to %i' %(i_min,i_max))
            fname2 = self._fname('sources','bootstrap','.npz',
                                '%i-%i' %(i_min,i_max),event,
                                'ar'*(ar and not keyword_out),keyword_out)
            if os.path.isfile(fname2) and not overwrite:
                continue
            for i,k in enumerate(tqdm(range(i_min,i_max))):
                indices = bootstrap_indices[k]
                bl_indices = [np.where(bl_events == j)[0][0]
                              for j in events[indices]]
                inv = self._generate_inverse(epochs,fwd,bl_epochs[bl_indices],
                                             lambda2,method,pick_ori)
                evoked = epochs[indices].average()
                stc = apply_inverse(evoked,inv,lambda2=lambda2,method=method,
                                           pick_ori=pick_ori)
                stcs[i] = stc.data[:]
                if tfr:
                    this_tfr = mne.time_frequency.tfr.cwt(stc.data.copy(),Ws,
                                                          use_fft=use_fft,
                                                          mode=mode)
                    power = (this_tfr * this_tfr.conj()).real
                    if itc:
                        this_itc = np.angle(this_tfr)
                    for band,inds in band_inds.items():
                        powers[band][i] = power[:,inds].mean(axis=1)
                        if itc:
                            itcs[band][i] = this_itc[:,inds].mean(axis=1)
            np.savez_compressed(fname2,stcs=stcs)
            if tfr:
                for band in bands:
                    fname3 = self._fname('sources','bootstrap_power_%s' %(band),
                                         '.npz','%i-%i' %(i_min,i_max),event,
                                         'ar'*(ar and not keyword_out),keyword_out)
                    np.savez_compressed(fname3,powers=powers[band])
                    if itc:
                        fname4 = self._fname('sources','bootstrap_itc_%s' %(band),
                                             '.npz','%i-%i' %(i_min,i_max),event,
                                             'ar'*(ar and not keyword_out),keyword_out)
                        np.savez_compressed(fname4,itcs=itcs[band])
        inv = self._generate_inverse(epochs,fwd,bl_epochs,lambda2,method,
                                     pick_ori)
        evoked = epochs.average()
        stc = apply_inverse(evoked,inv,lambda2=lambda2,method=method,
                            pick_ori=pick_ori)
        np.savez_compressed(fname,events=events,batch=batch,tfr=tfr,itc=itc,
                            freqs=freqs,n_cycles=n_cycles,bands=bands,
                            bootstrap_indices=bootstrap_indices)
        self._save_source(stc,event,'Bootstrap','base',ar=ar,keyword=keyword_out)

    def sourceCorrelation(self,event,condition,ar=False,keyword_in=None,
                          keyword_out=None,baseline=(-0.5,-0.1),
                          n_permutations=1000,overwrite=False):
        ''' baseline only supported in source window (this also means
            locked to the same event as the source estimate) due to
            normalization issues.
            n_permutations = 1000 takes ~5 hours with tfr'''
        keyword_out = keyword_in if keyword_out is None else keyword_out
        fname_in = self._fname('sources','bootstrap','.npz',event,
                               'ar'*(ar and not keyword_out),keyword_in)
        fname_out = self._fname('sources','correlation','.npz',event,
                                'ar'*(ar and not keyword_out),keyword_in)
        if not os.path.isfile(fname_in):
            raise ValueError('Bootstraps must be computed first' +
                             '(check that keywords match)')
        if os.path.isfile(fname_out) and not overwrite:
            raise ValueError('Correlations already exist, use overwrite=True')
        f = np.load(fname_in)
        bootstrap_indices = f['bootstrap_indices']
        Nboot,Nave = bootstrap_indices.shape
        batch = f['batch'].item()
        bands = f['bands'].item()
        tfr = f['tfr'].item()
        itc = f['itc'].item()
        events = f['events']
        bootstrap_conditions = []
        for i in range(Nboot):
            bootstrap_conditions.append(
                np.nanmean(np.array([self.behavior[condition][j]
                                    for j in bootstrap_indices[i]])))
        stc = self._load_source(event,'Bootstrap','base',ar=ar,keyword=keyword_in)
        if baseline[0] < stc.tmin or baseline[1] > stc.times[-1]:
            raise ValueError('Baseline outside time range')
        nSRC,nTIMES = stc.shape
        bl_indices = np.where((stc.times >= baseline[0]) &
                              (stc.times < baseline[1]))[0]
        permutation_indices = np.random.randint(0,Nboot,(n_permutations,Nboot))
        stc_copy = stc.copy()
        stc_copy.data.fill(0)
        #
        stcs = np.memmap('sb_%s_%s_%s_workfile' %(event,ar,keyword_out),
                         dtype='float64', mode='w+',
                         shape=(Nboot,nSRC,nTIMES))
        stc_result = stc_copy.copy()
        bl_dist = np.zeros((stc.data.shape[0],n_permutations))
        if tfr:
            powers = {band:np.memmap(('sb_power_%s_%s_%s_%s_workfile'
                                      %(event,ar,keyword_out,band)),
                                     dtype='float64', mode='w+',
                                     shape=(Nboot,nSRC,nTIMES))
                      for band in bands}
            power_result = {band:stc_copy.copy() for band in bands}
            power_bl_dist = {band:np.zeros((stc.data.shape[0],n_permutations))
                             for band in bands}
            if itc:
                itcs = {band:np.memmap(('sb_itc_%s_%s_%s_%s_workfile'
                                        %(event,ar,keyword_out,band)),
                                        dtype='float64', mode='w+',
                                        shape=(Nboot,nSRC,nTIMES))
                        for band in bands}
                itc_result = {band:stc_copy.copy() for band in bands}
                itc_bl_dist = {band:np.zeros((stc.data.shape[0],n_permutations))
                               for band in bands}
        mins = range(0,Nboot-batch +1,batch)
        maxs = range(batch,Nboot+1,batch)
        for i_min,i_max in zip(mins,maxs):
            print('Combining bootstraps %i to %i source' %(i_min,i_max),end='')
            fname2 = self._fname('sources','bootstrap','.npz',
                                 '%i-%i' %(i_min,i_max),event,
                                 'ar'*(ar and not keyword_out),keyword_out)
            stcs2 = np.load(fname2)['stcs']
            for i,j in enumerate(range(i_min,i_max)):
                stcs[j] = stcs2[i]
            del stcs2
            if tfr:
                for band in bands:
                    print(' %s tfr' %(band), end='')
                    fname3 = self._fname('sources','bootstrap_power_%s' %(band),
                                         '.npz','%i-%i' %(i_min,i_max),event,
                                         'ar'*(ar and not keyword_out),keyword_out)
                    powers2 = np.load(fname3)['powers']
                    if itc:
                        print(' %s itc' %(band), end='')
                        fname4 = self._fname('sources','bootstrap_itc_%s' %(band),
                                             '.npz','%i-%i' %(i_min,i_max),event,
                                             'ar'*(ar and not keyword_out),keyword_out)
                        itcs2 = np.load(fname4)['itcs']
                    for i,j in enumerate(range(i_min,i_max)):
                        powers[band][j] = powers2[i]
                        if itc:
                            itcs[band][j] = itcs2[i]
                    del powers2
                    if itc:
                        del itcs2
            print(' Done.')
        # Get baseline distribution
        for s_ind in tqdm(range(nSRC)):
            s_data = stcs[:,s_ind]
            if tfr:
                power_s_data = {band:powers[band][:,s_ind] for band in bands}
                if itc:
                    itc_s_data = {band:itcs[band][:,s_ind] for band in bands}
            for p_ind in range(n_permutations):
                dist = s_data[:,bl_indices].mean(axis=1)
                _,_,r,_,_ = linregress(dist[permutation_indices[p_ind]],
                                       bootstrap_conditions)
                bl_dist[s_ind,p_ind] = r
                if tfr:
                    for band in bands:
                        power_dist = power_s_data[band][:,bl_indices].mean(axis=1)
                        _,_,r,_,_ = linregress(power_dist[permutation_indices[p_ind]],
                                               bootstrap_conditions)
                        power_bl_dist[band][s_ind,p_ind] = r
                        if itc:
                            itc_dist = itc_s_data[band][:,bl_indices].mean(axis=1)
                            _,_,r,_,_ = linregress(itc_dist[permutation_indices[p_ind]],
                                                   bootstrap_conditions)
                            itc_bl_dist[band][s_ind,p_ind] = r
        # Get p-values from permutation distribution
        for s_ind in tqdm(range(nSRC)):
            s_data = stcs[:,s_ind]
            if tfr:
                power_s_data = {band:powers[band][:,s_ind] for band in bands}
                if itc:
                    itc_s_data = {band:itcs[band][:,s_ind] for band in bands}
            for t_ind in range(nTIMES):
                dist = s_data[:,t_ind]
                _,_,r,_,_ = linregress(dist,bootstrap_conditions)
                p = sum(abs(bl_dist[s_ind])>abs(r))/n_permutations
                stc_result.data[s_ind,t_ind] = ((1.0/p)*np.sign(r) if p > 0 else
                                                (1.0/n_permutations)*np.sign(r))
                if tfr:
                    for band in bands:
                        power_dist = power_s_data[band][:,t_ind]
                        _,_,r,_,_ = linregress(power_dist,bootstrap_conditions)
                        p = sum(abs(power_bl_dist[band][s_ind])>abs(r))/n_permutations
                        power_result[band].data[s_ind,t_ind] = \
                            ((1.0/p)*np.sign(r) if p > 0 else
                             (1.0/n_permutations)*np.sign(r))
                        if itc:
                            itc_dist = itc_s_data[band][:,t_ind]
                            _,_,r,_,_ = linregress(itc_dist,bootstrap_conditions)
                            p = sum(abs(itc_bl_dist[band][s_ind])>abs(r))/n_permutations
                            itc_result[band].data[s_ind,t_ind] = \
                                ((1.0/p)*np.sign(r) if p > 0 else
                                 (1.0/n_permutations)*np.sign(r))
        self._save_source(stc_result,event,condition,'correlation',
                          ar=ar,keyword=keyword_out)
        if tfr:
            for band in bands:
                self._save_source(power_result[band],event,condition,
                                  'power_correlation_%s' %(band),ar=ar,
                                  keyword=keyword_out)
                if itc:
                    self._save_source(itc_result[band],event,condition,
                                      'itc_correlation_%s' %(band),ar=ar,
                                      keyword=keyword_out)

    def noreunPhi(self,event,condition,values=None,ar=False,keyword_in=None,
                  keyword_out=None,tmin=None,tmax=None,npoint_art=0,
                  Nboot=480,alpha=0.01,downsample=True,seed=11,
                  shared_baseline=False,fs_av=False,bl_tmin=-0.5,bl_tmax=-0.1,
                  recalculate_baseline=False,recalculate_PCI=False):
        ''' note: has to have baseline-event continuity for source space
            transformation: cannot use baseline as would be defined for
            other applications based on another event. If there is a task and the
            baseline needs to be related to another event (e.g. the start of a
            stimulus compared to the PCI being relative to the response)
            the recommended solution is longer epochs'''
        if downsample:
            np.random.seed(seed)
        keyword_out = keyword_in if keyword_out is None else keyword_out
        tmin_condition = tmin
        tmax_condition = tmax # recorded if a condition is used instead of a value
        tmin,tmax = self._default_t(event,tmin,tmax)
        epochs = self._load_epochs(event,ar=ar,keyword=keyword_in)
        epochs = epochs.pick_types(meg=self.meg,eeg=self.eeg)
        epochs = epochs.crop(tmin=min([tmin,bl_tmin]),tmax=max([tmax,bl_tmin]))
        if self.eeg:
            epochs = epochs.set_eeg_reference(ref_channels='average',
                                              projection=True,verbose=False)
        info = epochs.info
        values = self._default_values(values,condition)
        value_indices = self._get_indices(epochs,condition,values)
        bl_tind = np.intersect1d(np.where(bl_tmin<=epochs.times),
                                 np.where(epochs.times<=bl_tmax))
        nTR = min([len(value_indices[value]) for value in value_indices])

        def preprocess(epochs,indices,bl_tind,event,condition,value,nTR,ar,keyword):
            Y = epochs[indices].get_data()
            inv,lambda2,method,pick_ori = \
                self._load_inverse(event,condition,value,ar=ar,keyword=keyword)
            nSRC = inv['nsource']
            nTIME = len(epochs.times)
            J = apply_inverse(epochs[indices].average(),inv,
                   method=method,lambda2=lambda2,
                   pick_ori=pick_ori,verbose=False).data
            basecorr = np.mean(J[:,bl_tind],axis=1)
            N0 = len(bl_tind)
            Norm = np.std(J[:,bl_tind],axis=1,ddof=1)
            J-=np.kron(np.ones((1,nTIME)),basecorr.reshape((nSRC,1)))
            NUM = np.kron(np.ones((1,N0)),basecorr.reshape((nSRC,1)))
            DEN = np.kron(np.ones((1,N0)),Norm.reshape((nSRC,1)))
            return Y,J,inv,lambda2,method,pick_ori,NUM,DEN,Norm

        def downsampleIndices(indices,nTR,condition,value):
            print('Subsampling %i/%i ' %(nTR,len(indices)) +
                  'for %s %s.' %(condition,value))
            np.random.shuffle(indices)
            indices = indices[:nTR]
            indices = sorted(indices)
            return indices

        def baseline_bootstrap(Y,J,bl_tind,Norm,NUM,DEN,Nboot,alpha,
                               info,inv,lambda2,method,pick_ori):
            nTR,nCH,nTIME = Y.shape
            nSRC = J.shape[0]
            N0 = len(bl_tind)
            randontrialsT=np.random.randint(0,nTR,nTR)
            Bootstraps=np.zeros((Nboot,N0))
            for per in tqdm(range(Nboot)):
                YT=np.zeros((nTR,nCH,N0))
                for j in range(nTR):
                    randonsampT = np.random.choice(bl_tind,N0,replace=True)#np.random.randint(0,N0,N0)
                    YT[j] = Y[randontrialsT[j]][:,randonsampT]
                YTE = EpochsArray(YT,info,verbose=False)
                ET=apply_inverse(YTE.average(),inv,method=method,
                                 lambda2=lambda2,pick_ori=pick_ori,verbose=False).data
                ET=(ET-NUM)/DEN # computes a Z-value
                Bootstraps[per,:] = np.max(np.abs(ET),axis=0) # maximum statistics in space
            # computes threshold for binarization depending on alpha value
            Bootstraps=np.sort(np.reshape(Bootstraps,(Nboot*N0)))
            calpha=1-alpha
            calpha_index=int(np.floor(calpha*Nboot*N0))
            TT=Norm*Bootstraps[calpha_index]# computes threshold based on alpha set before
            Threshold=np.kron(np.ones((1,nTIME)),TT.reshape((nSRC,1)))
            return Threshold

        def threshold_50_50(J,tind):
            Threshold = np.zeros((J.shape))
            for i in range(J.shape[0]):
                Threshold[i,:] = np.median(abs(J[i,tind]))
            return Threshold

        def gettind(epochs,tmin,tmax,npoint_art,value):
             # setup for if a dynamic tmin/max is to be used
            if type(tmin) is dict:
                tmin = tmin[value]
            if type(tmax) is dict:
                tmax = tmax[value]
            tind = np.intersect1d(np.where(tmin<=epochs.times),
                                 np.where(epochs.times<=tmax))
            return tind[npoint_art:]

        def computePCI(binJ):
            print('Computing LZ complexity...')
            ct=pci.lz_complexity_2D(binJ)
            print('Computing the normalization factor...')
            norm=pci.pci_norm_factor(binJ)
            ct=ct/norm
            print('Done.')
            return ct

        for value in ['all'] if shared_baseline else values:
            ''' Use separate baselines to threshold for significance because
            what is significant for sleep may not be for wake ect'''
            if (os.path.isfile(self._fname('noreun_phi','Threshold','.npz',
                                           'ar'*(False if keyword_out else ar),
                                           keyword_out,event,condition,value)) and
                not recalculate_baseline):
                print('Loading pre-computed bootstraps')
            else:
                indices = value_indices[value]
                if downsample:
                    indices = downsampleIndices(indices,nTR,condition,value)
                Y,J,inv,lambda2,method,pick_ori,NUM,DEN,Norm = \
                    preprocess(epochs,indices,bl_tind,event,condition,value,
                               nTR,ar,keyword_in)
                if alpha == '50_50':
                    tind = gettind(epochs,tmin,tmax,npoint_art,value)
                    Threshold = threshold_50_50(J,tind)
                else:
                    Threshold = baseline_bootstrap(Y,J,bl_tind,Norm,NUM,DEN,Nboot,
                                                   alpha,info,inv,lambda2,method,
                                                   pick_ori)
                self._save_noreun_baseline(Y,J,Threshold,epochs[indices].events[:,2],
                                           bl_tmin,bl_tmax,Nboot,alpha,event,
                                           condition,value,ar,keyword_out)
        # PCI by value
        for value in values:
            if ((os.path.isfile(self._fname('noreun_phi','pci','.npz',
                                'ar'*(False if keyword_out else ar),
                                keyword_out,event,condition,value)))
                 and not recalculate_PCI):
                print('PCI already computed')
            else:
                Y,J,Threshold,events,bl_tmin,bl_tmax,Nboot,alpha = \
                    self._load_noreun_baseline(event,condition,
                                               'all' if shared_baseline else value,
                                               ar,keyword_out)
                tind = gettind(epochs,tmin,tmax,npoint_art,value)
                #J = J[:,tind]
                # determines sources matrices
                binJ=np.array(np.abs(J)>Threshold,dtype=int)
                # rank the activity matrix - use mergesort that yields same results of Matlab
                Irank=np.argsort(np.sum(binJ,axis=1),kind='mergesort')
                binJrank=np.copy(binJ)
                binJrank=binJ[Irank,:]
                binJ=binJrank[:,tind]
                ct = computePCI(binJ)
                self._save_noreun_PCI(ct,binJ,tmin,tmax,npoint_art,
                                      event,condition,value,ar,keyword_out)

    def _load_noreun_baseline(self,event,condition,value,ar=False,keyword=None):
        fname = self._fname('noreun_phi','Threshold','.npz',
                            'ar'*(False if keyword else ar),keyword,
                            event,condition,value)
        if os.path.isfile(fname):
            f = np.load(fname)
            try:
                return (f['Y'],f['J'],f['Threshold'],f['events'],f['bl_tmin'].item(),
                        f['bl_tmax'].item(),f['Nboot'].item(),f['alpha'].item())
            except:
                return (f['Y'],f['J'],f['Threshold'],f['bl_tmin'].item(),
                        f['bl_tmax'].item(),f['Nboot'].item(),f['alpha'].item())
        else:
            raise ValueError('Threshold not computed for %s %s %s %s %s'
                             %(event,condition,value,'ar'*ar,
                               keyword if keyword is not None else ''))

    def _save_noreun_baseline(self,Y,J,Threshold,events,bl_tmin,bl_tmax,Nboot,alpha,
                              event,condition,value,ar=False,keyword=None):
        fname = self._fname('noreun_phi','Threshold','.npz',
                            'ar'*(False if keyword else ar),keyword,
                            event,condition,value)
        np.savez_compressed(fname,Y=Y,J=J,Threshold=Threshold,bl_tmin=bl_tmin,
                            bl_tmax=bl_tmax,events=events,Nboot=Nboot,alpha=alpha)

    def _load_noreun_PCI(self,event,condition,value,ar=False,keyword=None):
        fname = self._fname('noreun_phi','pci','.npz',
                            'ar'*(False if keyword else ar),keyword,
                            event,condition,value)
        if os.path.isfile(fname):
            print('Loading PCI for %s %s %s' %(event,condition,value))
            f = np.load(fname)
        else:
            raise ValueError('%s %s %s %s %s PCI not calculated'
                             %(event,condition,value,'ar'*ar,
                                keyword if keyword is not None else ''))
        return f['ct'],f['binJ'],f['tmin'].item(),f['tmax'].item(),f['npoint_art'].item()

    def _save_noreun_PCI(self,ct,binJ,tmin,tmax,npoint_art,event,condition,value,
                         ar=False,keyword=None):
        print('Saving noreun PCI for %s %s %s' %(event,condition,value))
        fname = self._fname('noreun_phi','pci','.npz',
                            'ar'*(False if keyword else ar),keyword,
                            event,condition,value)
        np.savez_compressed(fname,ct=ct,binJ=binJ,tmin=tmin,tmax=tmax,
                            npoint_art=npoint_art)

    def plotNoreunPCI(self,event,condition,values=None,ar=False,keyword=None,
                      ssm=True,pci=True,downsampled=True,shared_baseline=False,
                      fontsize=24,wspace=0.4,linewidth=4,show=True):
        values = self._default_values(values,condition)
        if len(values) > 1:
            fig, axs = plt.subplots(1,len(values))
        else:
            fig, ax = plt.subplots()
            axs = [ax]
        fig.set_size_inches(12,8)
        fig.subplots_adjust(wspace=wspace)
        yMAX = 0
        for i,value in enumerate(values):
            ct,binJ,tmin,tmax,npoint_art = \
                self._load_noreun_PCI(event,condition,value,ar,keyword)
            Y,J,Threshold,events,bl_tmin,bl_tmax,Nboot,alpha = \
                    self._load_noreun_baseline(event,condition,
                                               'all' if shared_baseline else value,
                                               ar,keyword)
            start = float(npoint_art)/(npoint_art + ct.shape[0])*(tmax-tmin)
            if ct.max() > yMAX:
                yMAX = ct.max()
            if ssm:
                ax = axs[i].twinx() if pci else axs[i]
                ax.zorder = 0
                nSRC,nTIME = binJ.shape
                ax.imshow(binJ,extent=[0,nTIME,0,nSRC],
                          aspect='auto',cmap='Greys')
                ax.set_ylim(ymin=-10,ymax=nSRC+10)
                ax.set_ylabel('Sources Ranked by Activity',fontsize=fontsize)
            if pci:
                ax = axs[i]
                ax.zorder = 1
                ax.patch.set_alpha(0)
                ax.plot(range(ct.shape[0]),ct,'y-',
                        linewidth=linewidth,alpha=1,zorder=1)
                ax.plot(range(ct.shape[0]),ct,'b-',
                        linewidth=linewidth/2,alpha=1,zorder=2)
                ax.set_ylabel('PCI',fontsize=fontsize)
            title = ('%s' %(value) + ', PCI=%2.2g' %(ct[-1])*pci +
                     ', %i Trials'%(Y.shape[0]))
            ax.set_title(title,fontsize=fontsize)
            ax.set_xlabel('Time (s)',fontsize=fontsize)
            ax.set_xlim([-10,ct.shape[0]+10])
            ax.set_xticks(np.linspace(0,ct.shape[0],5))
            ax.set_xticklabels(np.round(np.linspace(start,tmax,5),2))

        if pci: [ax.set_ylim(ymax=yMAX*1.05) for ax in axs]
        title = ('%s %s' %(event,condition) + ' Significant Sources'*ssm +
                 ' and'*(pci and ssm) + ' PCI'*pci + ' ar'*ar +
                 ' %s' %(keyword) * (keyword is not None))
        fig.suptitle(title,fontsize=fontsize)
        fig.savefig(self._fname('plots','noreun_phi_plot','.jpg','ar'*ar,
                                keyword,event,condition,*values))
        self._show_fig(fig,show)

    def raw2mat(self,preprocessed=False,ica=False,keyword=None,ch=None):
        raw = self._load_raw(preprocessed=preprocessed,ica=ica,keyword=keyword)
        if ch is None:
            ch_dict = self._get_ch_dict(raw)
        else:
            ch_dict = {raw.info['ch_names'].index(ch):ch}
        raw_data = raw.get_data()
        data_dict = {}
        for ch in ch_dict:
            data_dict[ch_dict[ch]] = raw_data[ch]
        savemat(self._fname('mat_files','data','.mat',
                            'preprocessed'*preprocessed,'ica'*ica,keyword),
                data_dict)

    def showConnectivity(self,event,condition,values=None,
                         bands={'theta':(4,8),'alpha':(8,15),'beta':(15,30)}):
        # NOT DONE
        values_dict = self._get_values(event,condition,values,
                                       contrast,tfr=False,band_struct=[],eog=False)
        epochs_data = self.epochs_current[event].get_data()
        indices = self.epochs_dict[event][condition][value]
        epochs_indices = [self.epochs_current[event].event_id[str(i)] for i in indices if
                            str(i) in self.epochs_current[event].event_id]
        this_chs = pick_types(self.epochs_current[event].info,meg=False,eeg=True)
        epochs_data = epochs_data[epochs_indices,eeg_chs]
        con, freqs, times, n_epochs, n_tapers = spectral_connectivity(
            epochs_data, method='pli', mode='multitaper',
            sfreq=self.epochs_current[event].info['sfreq'], fmin=fmin, fmax=fmax,
            faverage=True, tmin=self.events[event][1], mt_adaptive=False, n_jobs=1)

        # con is a 3D array where the last dimension is size one since we averaged
        # over frequencies in a single band. Here we make it 2D
        con = con[:, :, 0]

        # Now, visualize the connectivity in 3D
        from mayavi import mlab  # noqa

        mlab.figure(size=(600, 600), bgcolor=(0.5, 0.5, 0.5))

        # Plot the sensor locations
        sens_loc = [raw.info['chs'][picks[i]]['loc'][:3] for i in idx]
        sens_loc = np.array(sens_loc)

        pts = mlab.points3d(sens_loc[:, 0], sens_loc[:, 1], sens_loc[:, 2],
                            color=(1, 1, 1), opacity=1, scale_factor=0.005)

        # Get the strongest connections
        n_con = 20  # show up to 20 connections
        min_dist = 0.05  # exclude sensors that are less than 5cm apart
        threshold = np.sort(con, axis=None)[-n_con]
        ii, jj = np.where(con >= threshold)

        # Remove close connections
        con_nodes = list()
        con_val = list()
        for i, j in zip(ii, jj):
            if linalg.norm(sens_loc[i] - sens_loc[j]) > min_dist:
                con_nodes.append((i, j))
                con_val.append(con[i, j])

        con_val = np.array(con_val)

        # Show the connections as tubes between sensors
        vmax = np.max(con_val)
        vmin = np.min(con_val)
        for val, nodes in zip(con_val, con_nodes):
            x1, y1, z1 = sens_loc[nodes[0]]
            x2, y2, z2 = sens_loc[nodes[1]]
            points = mlab.plot3d([x1, x2], [y1, y2], [z1, z2], [val, val],
                                 vmin=vmin, vmax=vmax, tube_radius=0.001,
                                 colormap='RdBu')
            points.module_manager.scalar_lut_manager.reverse_lut = True


        mlab.scalarbar(title='Phase Lag Index (PLI)', nb_labels=4)

        # Add the sensor names for the connections shown
        nodes_shown = list(set([n[0] for n in con_nodes] +
                               [n[1] for n in con_nodes]))

        for node in nodes_shown:
            x, y, z = sens_loc[node]
            mlab.text3d(x, y, z, raw.ch_names[picks[node]], scale=0.005,
                        color=(0, 0, 0))

        view = (-88.7, 40.8, 0.76, np.array([-3.9e-4, -8.5e-3, -1e-2]))
        mlab.view(*view)

def _noreun_random_source(inv,lambda2,method,Y,
                          info,nSRC,N0,nTR,randontrialsT):
    randonsampT = np.random.randint(0,N0,N0)
    D = Y[randontrialsT]
    D = D[:,:,randonsampT]
    Epochs = EpochsArray(D,info,verbose=False)
    if self.eeg:
        Epochs = Epochs.set_eeg_reference(None,verbose=False)
    Evoked = Epochs.average()
    stc = apply_inverse(Evoked,inv,lambda2=lambda2,
                         method=method,pick_ori=pick_ori,verbose=False)
    return stc.data

def create_demi_events(raw, window_size, shift, epoches_nun=0):
    windows_length = raw.info['sfreq']*window_size
    windows_shift = raw.info['sfreq']*shift
    import math
    # T = raw._data.shape[1]
    T = raw.last_samp - raw.first_samp + 1
    if epoches_nun == 0:
        epoches_nun = int(math.floor((T - windows_length) / windows_shift + 1))
    demi_events = np.zeros((epoches_nun, 3), dtype=np.uint32)
    for win_ind in range(epoches_nun):
        demi_events[win_ind] = [win_ind * windows_shift, win_ind * windows_shift + windows_length, 0]
    # for win_ind, win in enumerate(range(0, max_time, W * 2)):
        # demi_events[win_ind * 2] = [win, win + W, 0]
        # demi_events[win_ind * 2 + 1] = [win + W + 1, win + W * 2, 1]
    # demi_events_ids = {'demi_1': 0, 'demi_2': 1}
    demi_events[:, :2] += raw.first_samp
    demi_conditions = {'demi': 0}
    return demi_events, demi_conditions


class Comparator:
    ''' Compares MEEG data generated by M/EEGbuddy for subject level comparisons.'''
    def __init__(self,data_struct,groups=None):
        # the data structure is a dictionary with M/EEGBuddies by name
        self.data_struct = data_struct
        self.groups = groups

    def plotnoreunPhiComparison(self,event,condition,values=None):
        values = self._default_values(values,condition)

    def _default_values(self,values,condition,contrast=False):
        values = None
