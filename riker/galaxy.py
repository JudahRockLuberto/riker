#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Deal with galaxy from Illustris or TNG simulation."""

import os
import shutil
import warnings

import numpy as np

import matplotlib.pyplot as plt

from astropy.table import Table, Column, join

from riker import utils
from riker import config
from riker import visual
from riker import profile

warnings.filterwarnings("ignore")

__all__ = [
    'GalaxyMap',
    'KERNEL'
    ]

# This is the detection kernel used in sep
KERNEL = np.asarray([[0.092163, 0.221178, 0.296069, 0.221178, 0.092163],
                     [0.221178, 0.530797, 0.710525, 0.530797, 0.221178],
                     [0.296069, 0.710525, 0.951108, 0.710525, 0.296069],
                     [0.221178, 0.530797, 0.710525, 0.530797, 0.221178],
                     [0.092163, 0.221178, 0.296069, 0.221178, 0.092163]])

# Useful columns from the Ellipse output
ELL_COL_USE = [
    'index', 'sma', 'intens', 'int_err', 'ell', 'ell_err', 'pa', 'pa_err',
    'x0', 'x0_err', 'y0', 'y0_err', 'stop', 'tflux_e', 'tflux_c',
    'pa_norm', 'growth_ori', 'sbp_err'
]


class GalaxyMap(object):
    """Dealing with 2-D projected map of a galaxy.

    Parameters
    ----------
    hdf5 : BeneMassAgeZMaps object
        HDF5 data for all the 2-D maps.
    idx : int
        Index of the galaxy to be analyzed.
    proj : str, optional
        Projection of the 2-D map. Default: 'xy'
    config_file : str, optional
        A yaml configuration file.
    rad : ndarray, optional
        Radial bins used for aperture measurements.
    aper_force : dict, optional
        Dictionary that contains external shape information of the galaxy. Default: None.

    """

    def __init__(self, hdf5, idx, proj='xy', config_file=None,
                 rad=None, aper_force=None):
        """Gather basic information and all the maps.
        """
        # Parent directory to keep the files
        self.dir = hdf5.dir

        # Other directories for saving output files
        self.fits_dir = os.path.join(self.dir, 'fits')
        self.fig_dir = os.path.join(self.dir, 'fig')
        self.sum_dir = os.path.join(self.dir, 'sum')
        if not os.path.isdir(self.fits_dir):
            os.mkdir(self.fits_dir)
        if not os.path.isdir(self.fig_dir):
            os.mkdir(self.fig_dir)
        if not os.path.isdir(self.sum_dir):
            os.mkdir(self.sum_dir)

        # Pixel scale in unit of kpc per pixel
        self.pix = hdf5.pix

        # Redshift of the snapshot
        self.redshift = hdf5.redshift

        # Index of the galaxy
        self.idx = idx

        # Projection of the 2-D map
        self.proj = proj

        # Configuration parameters
        self.config = config.BeneMassAgeZConfig(config_file=config_file)

        # Gather all the maps and the basic information
        self.info, self.maps = hdf5.get_maps(self.idx, self.proj)

        # Prefix for output files
        self.prefix = "{}_{}_{}_{}".format(
            hdf5.label, self.idx, self.info['catsh_id'], self.proj)

        # Design the radial bins
        self.rad_bins = rad
        self.rad_inn = None
        self.rad_out = None
        self.rad_mid = None
        self.radial_bins(rad=rad)

        # Externally provided galaxy information
        self.aper_force = aper_force

        # Placeholder for results
        # Basic information
        self.detect_gal = None
        self.detect_ins = None
        self.detect_exs = None

        # Aperture stellar mass profiles
        self.maper_gal = None
        self.maper_ins = None
        self.maper_exs = None

        # Age profiles
        self.age_prof_gal = None
        self.age_prof_ins = None
        self.age_prof_exs = None

        # Metallicity profiles
        self.met_prof_gal = None
        self.met_prof_ins = None
        self.met_prof_exs = None

        # Ellipse results for the whole galaxy
        self.ell_shape_gal = None
        self.ell_mprof_gal = None
        self.bin_mprof_gal = None

        # Ellipse results for the in-situ component
        self.ell_shape_ins = None
        self.ell_mprof_ins = None

        # Ellipse results for the ex-situ component
        self.ell_shape_exs = None
        self.ell_mprof_exs = None

        # Summary of results
        self.aper_sum = None
        self.ell_sum = None

    def radial_bins(self, rad=None, output=False):
        """Design radial bins to get aperture profiles.

        Parameters
        ----------
        rad : ndarray, optional
            Array of boundaries for radius bins in unit of kpc. Default: None.
        output : bool, optional
            Return the `detect` dictionary when True. Default: False

        Configuration Parameters
        ------------------------
        Can be found in `self.config`:
        n_rad : int, optional
            Number of radial bins. Default: 15.
        linear : bool, optional
            If True, radial bins will be uniformly spaced in linear space.
            If False, radial bins will be uniformly spaced in log10 space.
            Default: False
        r_min : float, optional
            Minimum radius of the radial bins. Default: 0.1
        r_max : float, optional
            Maximum radius of the radial bins. Default: None.

        """
        # Get the radial bins in unit of kpc
        if rad is None:
            if self.config.r_max is None:
                self.config.r_max = self.info['img_w'] / 2.0
            if self.config.linear:
                rad = np.linspace(
                    self.config.r_min, self.config.r_max * self.info['pix'],
                    (self.config.n_rad + 1))
            else:
                rad = np.logspace(
                    np.log10(self.config.r_min),
                    np.log10(self.config.r_max * self.info['pix']),
                    (self.config.n_rad + 1))

        # Arrays of inner and outer radius
        r_inn, r_out = rad[:-1], rad[1:]

        # Use the mid point of the radial bins
        r_mid = (r_inn + r_out) / 2.0

        setattr(self, 'rad_bins', rad)
        setattr(self, 'rad_inn', r_inn)
        setattr(self, 'rad_out', r_out)
        setattr(self, 'rad_mid', r_mid)

        if output:
            return rad

    def detect(self, map_type, verbose=False, kernel=KERNEL, output=False, **detect_kwargs):
        """Detect the galaxy and get basic properties.

        Parameters
        ----------
        map_type : str
            Type of the stellar mass map. Options ['gal'|'ins'|'exs']
        kernel : ndarray, optional
            2-D kernel used for detecting the galaxy. Default: None
        verbose : bool, optional
            Blah, Blah, Blah. Default: False
        output : bool, optional
            Return the `detect` dictionary when True. Default: False

        Configuration Parameters
        ------------------------
        Can be found in `self.config`:
        threshold : float, optional
            Mass threshold for detecting the galaxy. Default: 1E8
        bkg_ratio : int, optional
            Ratio between the image size and sky box size. Default: 10
        bkg_filter : int, optional
            Filter size for sky background. Default: 5

        Return
        ------
        detect : dict
            Dictionary that contains the basic information of this component.

        """
        # Basic geometry information of the galaxy
        # Here we have three configuration parameters:
        # threshold, bkg_ratio, bkg_filter
        try:
            detect = profile.detect_galaxy(
                self.info, self.maps["mass_{}".format(map_type)], kernel=kernel,
                threshold=self.config.threshold, bkg_ratio=self.config.bkg_ratio,
                bkg_filter=self.config.bkg_filter, **detect_kwargs)
        except ValueError:
            # Sometimes the pre-defined threshold can be too high for the galaxy
            new_threshold = np.nanmax(self.maps["mass_{}".format(map_type)]) / 20.0
            detect = profile.detect_galaxy(
                self.info, self.maps["mass_{}".format(map_type)], kernel=kernel,
                threshold=new_threshold, bkg_ratio=self.config.bkg_ratio,
                bkg_filter=self.config.bkg_filter, **detect_kwargs)

        if verbose:
            print("# Detection for {}".format(map_type))
            print(detect)

        setattr(self, 'detect_{}'.format(map_type), detect)

        if output:
            return detect

    def maper(self, map_type, verbose=False, output=False, **detect_kwargs):
        """Get aperture stellar mass curves from the stellar mass map.

        Parameters
        ----------
        map_type : str
            Type of the stellar mass map. Options ['gal'|'ins'|'exs']
        verbose : bool, optional
            Blah, Blah, Blah. Default: False
        output : bool, optional
            Return the `maper` array when True. Default: False

        Configuration Parameters
        ------------------------
        Can be found in `self.config`:
        using_gal : bool, optional
            Using the basic information of the whole galaxy. Default: True.
        subpix : int, optional
            Subpixel sampling factor. Default is 5.

        """
        # Get the basic information
        detect = getattr(self, "detect_{}".format(map_type))
        if map_type is not 'gal' and self.config.using_gal:
            detect = getattr(self, 'detect_gal')

        # If not basic information is available, run the detection again.
        if detect is None:
            if self.config.using_gal:
                detect = self.detect('gal', output=True, **detect_kwargs)
            else:
                detect = self.detect(map_type, output=True, **detect_kwargs)

        # Here we have 1 configuration parameter:
        # subpix
        _, maper = profile.aperture_masses(
            self.info, self.maps['mass_{}'.format(map_type)],
            detect=detect, rad=self.rad_out, subpix=self.config.subpix)

        if verbose:
            print("# Aperture masses for {}".format(map_type))
            print(maper)

        setattr(self, 'maper_{}'.format(map_type), maper)

        if output:
            return maper

    def aprof(self, data_type, map_type, output=False, verbose=False, return_mass=False,
              **detect_kwargs):
        """Get the average profiles of a property using pre-defined apertures.

        Parameters
        ----------
        data_type : str
            Galaxy property to be used. `age` for stellar age, `met` for stellar metallicity.
        map_type : str
            Type of the stellar mass map. Options ['gal'|'ins'|'exs']
        verbose : bool, optional
            Blah, Blah, Blah. Default: False
        output : bool, optional
            Return the `maper` array when True. Default: False
        return_mass : bool, optional
            Return the stellar mass in each radial bins. Default: False

        Configuration Parameters
        ------------------------
        Can be found in `self.config`:
        using_gal : bool, optional
            Using the basic information of the whole galaxy. Default: True.
        subpix : int, optional
            Subpixel sampling factor. Default: 5.

        """
        # Get the basic information
        detect = getattr(self, 'detect_{}'.format(map_type))
        if map_type is not 'gal' and self.config.using_gal:
            detect = getattr(self, 'detect_gal')

        # If not basic information is available, run the detection again.
        if detect is None:
            if self.config.using_gal:
                detect = self.detect('gal', output=True, **detect_kwargs)
            else:
                detect = self.detect(map_type, output=True, **detect_kwargs)

        mask = (self.maps['mass_gal'] < 1.).astype(np.uint8)

        prof = profile.mass_weighted_prof(
            self.maps['{}_{}'.format(data_type, map_type)],
            self.maps['mass_{}'.format(map_type)], detect, self.rad_inn, self.rad_out,
            subpix=self.config.subpix, return_mass=return_mass, mask=mask)

        if verbose:
            print("# {} profile for {}".format(data_type, map_type))
            print(prof)

        setattr(self, "{}_prof_{}".format(data_type, map_type), prof)

        if output:
            return prof

    def aper_summary(self, gal_only=False, output=False):
        """Get all the stellar mass, age, and metallicity profiles.

        Parameters
        ----------
        gal_only: bool, optional
            Only provide summary of the whole galaxy. Default: False.
        output : bool, optional
            Return the `maper` array when True. Default: False

        Configuration Parameters
        ------------------------
        Can be found in `self.config`:
        subpix : int, optional
            Subpixel sampling factor. Default is 5.

        """
        aper_sum = Table()
        
        # add expected values
        aper_sum.add_column(Column(data=self.rad_inn, name='rad_inn'))
        aper_sum.add_column(Column(data=self.rad_out, name='rad_out'))
        aper_sum.add_column(Column(data=self.rad_mid, name='rad_mid'))

        
        # aperture mass profiles if there and add to aper_sum table
        if 'map_star_rho_insitu_{}'.format(self.proj) in hdf5.hdf5_values and 'map_star_rho_exsitu_{}'.format(self.proj) in hdf5.hdf5_values:
            self.maper('gal')
            
            aper_sum.add_column(Column(data=self.maper_gal, name='maper_gal'))
            
            if not gal_only:
                self.maper('ins')
                self.maper('exs')
               
                aper_sum.add_column(Column(data=self.maper_ins, name='maper_ins'))
                aper_sum.add_column(Column(data=self.maper_exs, name='maper_exs'))
        else:
            print('Data for map_star_rho_insitu and map_star_rho_exsitu is not in the file. Skipped.')

        # aperture age profiles if there and add to aper_sum table
        if 'map_star_age_insitu_{}'.format(self.proj) in hdf5.hdf5_values and 'map_star_age_exsitu_{}'.format(self.proj) in hdf5.hdf5_values:
            self.aprof('age', 'gal', return_mass=True)
            
            aper_sum.add_column(Column(data=self.age_prof_gal['prof_w'], name='age_gal_w'))
            aper_sum.add_column(Column(data=self.age_prof_gal['prof'], name='age_gal'))
            aper_sum.add_column(Column(data=self.age_prof_gal['flag'], name='age_gal_flag'))
            aper_sum.add_column(Column(data=self.age_prof_gal['mass'], name='mprof_gal'))
            
            if not gal_only:
                self.aprof('age', 'ins', return_mass=True)
                self.aprof('age', 'exs', return_mass=True)

                aper_sum.add_column(Column(data=self.age_prof_ins['prof_w'], name='age_ins_w'))
                aper_sum.add_column(Column(data=self.age_prof_ins['prof'], name='age_ins'))
                aper_sum.add_column(Column(data=self.age_prof_ins['flag'], name='age_ins_flag'))
                aper_sum.add_column(Column(data=self.age_prof_ins['mass'], name='mprof_ins'))
                aper_sum.add_column(Column(data=self.age_prof_exs['prof_w'], name='age_exs_w'))
                aper_sum.add_column(Column(data=self.age_prof_exs['prof'], name='age_exs'))
                aper_sum.add_column(Column(data=self.age_prof_exs['flag'], name='age_exs_flag'))
                aper_sum.add_column(Column(data=self.age_prof_exs['mass'], name='mprof_exs'))
        else:
            print('map_star_age_insitu and map_star_age_insitu were not in file. Skipped.')

        # Aperture metallicity profiles if there and add to aper_sum table
        if 'map_star_metallicity_insitu_{}'.format(self.proj) in hdf5.hdf5_values and 'map_star_metallicity_exsitu_{}'.format(self.proj) in hdf5.hdf5_values:
            self.aprof('met', 'gal')
            
            aper_sum.add_column(Column(data=self.met_prof_gal['prof_w'], name='met_gal_w'))
            aper_sum.add_column(Column(data=self.met_prof_gal['prof'], name='met_gal'))
            aper_sum.add_column(Column(data=self.met_prof_gal['flag'], name='met_gal_flag'))
            
            if not gal_only:
                self.aprof('met', 'ins')
                self.aprof('met', 'exs')
                
                aper_sum.add_column(Column(data=self.met_prof_ins['prof_w'], name='met_ins_w'))
                aper_sum.add_column(Column(data=self.met_prof_ins['prof'], name='met_ins'))
                aper_sum.add_column(Column(data=self.met_prof_ins['flag'], name='met_ins_flag'))
                aper_sum.add_column(Column(data=self.met_prof_exs['prof_w'], name='met_exs_w'))
                aper_sum.add_column(Column(data=self.met_prof_exs['prof'], name='met_exs'))
                aper_sum.add_column(Column(data=self.met_prof_exs['flag'], name='met_exs_flag'))
        else:
            print('map_star_metallicity_insitu and map_star_metallicity_exsitu were not in file. Skipped.')

        setattr(self, 'aper_sum', aper_sum.as_array())

        if output:
            return aper_sum

    def map_to_fits(self, data_type, map_type, folder=None):
        """Save a 2-D map into a FITS file.

        Parameters
        ----------
        data_type : str
            Galaxy property to be used. `age` for stellar age, `met` for stellar metallicity.
        map_type : str
            Type of the stellar mass map. Options ['gal'|'ins'|'exs']
        folder : str, optional
            Output directory name. Default: None

        """
        # Output folder for the FITS file
        if folder is None:
            folder = self.fits_dir

        # Name of the output file
        fits_name = os.path.join(
            folder, "{}_{}_{}.fits".format(self.prefix, data_type, map_type))

        utils.save_to_fits(self.maps['{}_{}'.format(data_type, map_type)], fits_name)
        if not os.path.isfile(fits_name):
            raise FileNotFoundError("# Did not save the FITS file successfully!")

        return fits_name

    def ell_summary(self, gal_only=False, output=False):
        """Gather all necessary Ellipse profiles.

        Parameters
        ----------
        gal_only: bool, optional
            Only provide summary of the whole galaxy. Default: False.
        output : bool, optional
            Return the `maper` array when True. Default: False

        """
        # Ellipse run for the whole galaxy
        self.ell_prof('gal', remove_bin=False)

        if not gal_only:
            # Ellipse run for the in-situ component
            self.ell_prof('ins', remove_bin=True, in_ellip=self.bin_mprof_gal)

            # Ellipse run for the ex-situ component
            self.ell_prof('exs', remove_bin=True, in_ellip=self.bin_mprof_gal)

        ell_sum = {
            'gal_shape': self.ell_shape_gal, 'gal_mprof': self.ell_mprof_gal,
            'ins_shape': self.ell_shape_ins, 'ins_mprof': self.ell_mprof_ins,
            'exs_shape': self.ell_shape_exs, 'exs_mprof': self.ell_mprof_exs,
        }

        setattr(self, 'ell_sum', ell_sum)

        if output:
            return ell_sum

    def ell_prof(self, map_type, isophote=profile.ISO, xttools=profile.TBL,
                 remove_bin=False, in_ellip=None, output=False):
        """Run Ellipse on the stellar mass map.

        Parameters
        ----------
        map_type : str
            Type of the stellar mass map. Options ['gal'|'ins'|'exs']
        isophote : str, optional
            Location of the binary executable file: `x_isophote.e`. Default: ISO
        xttools : str, optional
            Location of the binary executable file: `x_ttools.e`. Default: TBL
        pix : float, optional
            Pixel scale. Default: 1.0.
        remove_bin : bool, optional
            Remove the output binary file or not. Default: False.
        in_ellip : str, optional
            Input binary table from previous Ellipse run. Default: None
        output : bool, optional
            Return the `maper` array when True. Default: False

        Configuration Parameters
        ------------------------
        Can be found in `self.config`:
        ini_sma : float, optional
            Initial radii to start the fitting. Default: 15.0.
        max_sma : float, optional
            Maximum fitting radius. Default: 175.0.
        step : float, optional
            Fitting step size foro Ellipse. Default: 0.2.
        mode : str, optional
            Integration mode for Ellipse fitting. Options: ['mean'|'median'|'bi-linear'].
            Default: 'mean'.

        """
        # Save the file to a FITS image
        fits_name = self.map_to_fits('mass', map_type, folder=None)

        # Get the isophotal shape and mass density profiles.
        ell_shape, ell_mprof, bin_shape, bin_mprof = profile.ell_prof(
            fits_name, self.detect_gal, isophote=isophote, xttools=xttools,
            pix=self.info['pix'], ini_sma=self.config.ini_sma,
            max_sma=self.config.max_sma, step=self.config.step,
            mode=self.config.mode, aper_force=self.aper_force, in_ellip=in_ellip)

        # Clean up a little
        folder, file_name = os.path.split(fits_name)
        utils.clean_after_ellipse(
            folder, file_name.replace('.fits', ''), remove_bin=remove_bin)

        # Sometimes Ellipse failed all the way...make sure it is dealt gracefully
        if ell_shape is None:
            print("# Ellipse fails to get shape for {} of {}".format(map_type, self.info['catsh_id']))
        else:
            # Calculate the Fourier amplitude information
            fourier_shape = profile.fourier_profile(ell_shape, pix=self.info['pix'])
            # Join the useful columns from the Ellipse output with the Fourier amplitudes.
            ell_shape_new = join(
                ell_shape[ELL_COL_USE], fourier_shape, keys='index', join_type='inner')
            setattr(self, 'ell_shape_{}'.format(map_type), ell_shape_new.as_array())

        if ell_mprof is None:
            print("# Ellipse fails to get profile for {} of {}".format(
                map_type, self.info['catsh_id']))
        else:
            # Calculate the Fourier amplitude information
            fourier_mprof = profile.fourier_profile(ell_mprof, pix=self.info['pix'])
            # Join the useful columns from the Ellipse output with the Fourier amplitudes.
            ell_mprof_new = join(
                ell_mprof[ELL_COL_USE], fourier_mprof, keys='index', join_type='inner')
            setattr(self, 'ell_mprof_{}'.format(map_type), ell_mprof_new.as_array())
            if map_type == 'gal':
                setattr(self, 'bin_mprof_gal', bin_mprof)

        if output:
            return ell_shape_new, ell_mprof_new, bin_shape, bin_mprof

    def show_maps(self, figsize=(15, 15), savefig=False, dpi=100):
        """Visualize the stellar mass, age, and metallicity maps for all components.

        Parameters
        ----------
        figsize : tuple, optional
            Size of the 3x3 figure. Default: (15, 15)
        savefig : bool, optional
            Save a copy of the figure in PNG format. Default: False.
        dpi : int, optional
            DPI value for saving PNG figure. Default: 100.

        """
        # Make sure the basic galaxy information is available
        if self.detect_gal is None:
            self.detect('gal')

        # Generate the figure
        map_fig = visual.show_maps(
            self.maps, self.detect_gal, figsize=figsize,
            cid=self.info['catsh_id'], logms=self.info['logms'])

        # Save the figure in PNG format if necessary
        if savefig:
            map_fig.savefig(
                os.path.join(self.fig_dir, "{}_maps.png".format(self.prefix)), dpi=dpi)
            plt.close(map_fig)
        else:
            return map_fig

    def show_aper(self, figsize=(8, 18), savefig=False, dpi=100, rad_min=5.5, rad_max=None):
        """Visualize the stellar mass, age, and metallicity aperture profiles
        for all components.

        Parameters
        ----------
        figsize : tuple, optional
            Size of the 3x3 figure. Default: (15, 15)
        savefig : bool, optional
            Save a copy of the figure in PNG format. Default: False.
        dpi : int, optional
            DPI value for saving PNG figure. Default: 100.
        rad_min : float, optional
            Minimum radius to plot, in unit of kpc. Default: 5.5.
        rad_max : float, optional
            Maximum radius to plot, in unit of kpc. Default: None

        """
        # Make sure the aperture profiles results are available
        if self.aper_sum is None:
            self.aper_summary(gal_only=False)

        # Maximum radii to plot
        if rad_max is None:
            rad_max = self.info['pix'] * self.info['img_w'] / 2.0

        # Generate the figure
        aper_fig = visual.show_aper(
            self.info, self.aper_sum, figsize=figsize, rad_min=rad_min, rad_max=rad_max)

        # Save the figure in PNG format if necessary
        if savefig:
            aper_fig.savefig(
                os.path.join(self.fig_dir, "{}_aper.png".format(self.prefix)), dpi=dpi)
            plt.close(aper_fig)
        else:
            return aper_fig

    def show_prof(self, savefig=False, dpi=100, z_min=3.5, z_max=10.5,
                  r_min=3.0, r_max=None, combine=False):
        """Visualize the 1-D Ellipse profiles of all components.

        Parameters
        ----------
        figsize : tuple, optional
            Size of the 3x3 figure. Default: (15, 15)
        savefig : bool, optional
            Save a copy of the figure in PNG format. Default: False.
        dpi : int, optional
            DPI value for saving PNG figure. Default: 100.
        z_min : float, optional
            Minimum log10(Mass) value used to show the stellar mass map. Default: 3.5
        z_max : float, optional
            Maximum log10(Mass) value used to show the stellar mass map. Default: 10.5
        r_min : float, optional
            Minimum radius to plot, in unit of kpc. Default: 3.0.
        r_max : float, optional
            Maximum radius to plot, in unit of kpc. Default: 190.
        combine : bool, optional
            Combine the two figures horizontally. Require `imagemagick`. Default: False.

        """
        # Make sure the Ellipse results are available
        if self.ell_sum is None:
            self.ell_summary()

        # Maximum radii to plot
        if r_max is None:
            r_max = self.info['pix'] * self.info['img_w'] / 2.0 * 1.2

        # Prepare the data used for making plots
        ell_plot = visual.prepare_show_ellipse(self.info, self.maps, self.ell_sum)

        # Making the overplot figure
        over_fig = visual.overplot_ellipse(ell_plot, zmin=z_min, zmax=z_max)

        # Making the 1-D profile figure
        prof_fig = visual.plot_ell_prof(ell_plot, r_min=r_min, r_max=r_max)

        # Save the figure in PNG format if necessary
        if savefig:
            over_file = os.path.join(self.fig_dir, "{}_over.png".format(self.prefix))
            prof_file = os.path.join(self.fig_dir, "{}_prof.png".format(self.prefix))
            over_fig.savefig(over_file, dpi=dpi)
            prof_fig.savefig(prof_file, dpi=dpi)
            plt.close(over_fig)
            plt.close(prof_fig)

            # Combine these two figures into one
            if combine:
                # This requires `imagemagick` is installed and `montage` command is available.
                montage = shutil.which('montage')
                if montage is None:
                    print("# Imagemagick is not available...")
                    return

                # Name of the combined figure
                ellip_file = os.path.join(self.fig_dir, "{}_ellp.png".format(self.prefix))

                # `montage` command
                cmd = "{} -mode concatenate -tile 2x1 -geometry +5 ".format(montage) + \
                    "{} {} {}".format(over_file, prof_file, ellip_file)
                os.system(cmd)

                if os.path.isfile(ellip_file):
                    os.remove(over_file)
                    os.remove(prof_file)
                else:
                    print("# Something went wrong with the command: {}".format(cmd))
                    return
        else:
            return over_fig, prof_fig

    def run_all(self, plot=False, output=False, save=True):
        """Run all the default analysis of a galaxy (and make plots).

        Parameters
        ----------
        plot : bool, optional
            Make some visualizations and save as PNG figures. Default: False.
        save : bool, optional
            Save the summary dict as a `.npy` file. Default: True
        output : bool, optional
            Also return the summary dict. Default: False

        """
        # Make sure the aperture profiles results are available
        if self.aper_sum is None:
            self.aper_summary(gal_only=False)

        # Make sure the Ellipse results are available
        if self.ell_sum is None:
            self.ell_summary(gal_only=False)

        # Make a summary data structure
        summary = {
            'info': self.info, 'geom': self.detect_gal,
            'aper': self.aper_sum, 'prof': self.ell_sum
        }

        # Visualize the results
        if plot:
            _ = self.show_maps(savefig=True)
            _ = self.show_aper(savefig=True)
            _ = self.show_prof(savefig=True, combine=True)

        if save:
            sum_file = os.path.join(self.sum_dir, "{}_sum.npy".format(self.prefix))
            utils.save_to_pickle(summary, sum_file)

        if output:
            return summary
