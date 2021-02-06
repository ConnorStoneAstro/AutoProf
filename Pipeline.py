import sys
import os
sys.path.append(os.environ['AUTOPROF'])
from autoprofutils.Background import Background_Global, Background_ByPatches, Background_ByIsophote, Background_Mode
from autoprofutils.Center import Center_Centroid, Center_OfMass, Center_Multi_Method, Center_1DGaussian, Center_Null, Center_HillClimb, Center_Forced
from autoprofutils.PSF import Calculate_PSF
from autoprofutils.Mask import Star_Mask_IRAF, NoMask, Overflow_Mask, Star_Mask_Given
from autoprofutils.Isophote_Initialize import Isophote_Initialize_GridSearch, Isophote_Initialize_CircFit
from autoprofutils.Elliptical_Isophotes import Fit_Isophotes
from autoprofutils.Isophote_Fit import Isophote_Fit_FFT_Robust, Isophote_Fit_FFT, Isophote_Fit_Forced
from autoprofutils.Check_Fit import Check_Fit_IQR, Check_Fit_Simple
from autoprofutils.Isophote_Extract import Isophote_Extract, Generate_Profile, Isophote_Extract_Forced
from autoprofutils.SharedFunctions import GetKwargs, Read_Image
from multiprocessing import Pool
from astropy.io import fits
from scipy.stats import iqr
from itertools import starmap
import numpy as np
from time import time, sleep
import traceback
import logging
import warnings
from astropy.io.fits.verify import VerifyWarning
warnings.simplefilter('ignore', category=VerifyWarning)

class Isophote_Pipeline(object):

    def __init__(self, loggername = None):
        """
        Initialize pipeline object, user can replace functions with their own if they want, otherwise defaults are used.

        loggername: String to use for logging messages
        """

        self.pipeline_functions = {'background': Background_Mode,
                                   'psf': Calculate_PSF,
                                   'center': Center_HillClimb,
                                   'isophoteinit': Isophote_Initialize_CircFit,
                                   'isophotefit': Isophote_Fit_FFT_Robust,
                                   'starmask': Star_Mask_IRAF,
                                   'isophoteextract': Isophote_Extract,
                                   'checkfit': Check_Fit_IQR}
        self.pipeline_steps = ['background', 'psf', 'center', 'isophoteinit', 'isophotefit', 'starmask', 'isophoteextract', 'checkfit']

        # Start the logger
        logging.basicConfig(level=logging.INFO, filename = 'AutoProf.log' if loggername is None else loggername, filemode = 'w')

    def UpdatePipeline(self, new_pipeline_functions = None, new_pipeline_steps = None):
        if new_pipeline_functions:
            self.pipeline_functions.update(new_pipeline_functions)
        if new_pipeline_steps:
            self.pipeline_steps = new_pipeline_steps

    def WriteProf(self, results, saveto, pixscale, name = None, **kwargs):
        """
        Writes the photometry information for disk given a photutils isolist object

        extractedprofile: surface brightness profile and all other data
        saveto: Full path string indicating where to save the profile
        pixscale: conversion factor between pixels and arcseconds (arcsec / pixel)
        starmask: Optional, a star mask to save along with the profile
        background: if saving the star mask, the background can also be saved
                    for full reproducability
        """

        with open(saveto + name + '.aux', 'w') as f:
            # write profile info
            f.write('name: %s\n' % str(name))
            f.write('pixel scale: %.3e arcsec/pix\n' % pixscale)
            for k in results['checkfit'].keys():
                f.write('check fit %s: %s\n' % (k, 'pass' if results['checkfit'][k] else 'fail'))
            f.write('psf median: %.3f pix, iqr: %.2e pix\n' % (results['psf']['fwhm'], results['psf']['iqr']))
            f.write('background median: %.3e flux, iqr: %.3e flux\n' % (results['background']['background'], results['background']['noise']))
            if 'center' in results['isophotefit']:
                use_center = results['isophotefit']['center']
            else:
                use_center = results['center']
            f.write('center x: %.2f pix, y: %.2f pix\n' % (use_center['x'], use_center['y']))
            f.write('global ellipticity: %.3f, pa: %.3f deg\n' % (results['isophoteinit']['ellip'], results['isophoteinit']['pa']*180/np.pi))
            if len(kwargs) > 0:
                for k in kwargs.keys():
                    f.write('settings %s: %s\n' % (k,str(kwargs[k])))
            
        # Write the profile
        with open(saveto + name + '.prof', 'w') as f:
            # Write profile header
            f.write(','.join(results['isophoteextract']['header']) + '\n')
            f.write(','.join(results['isophoteextract']['units'][h] for h in results['isophoteextract']['header']) + '\n')
            for i in range(len(results['isophoteextract']['data'][results['isophoteextract']['header'][0]])):
                line = list((results['isophoteextract']['format'][h] % results['isophoteextract']['data'][h][i]) for h in results['isophoteextract']['header'])
                f.write(','.join(line) + '\n')
                
        # Write the mask data, if provided
        if not results['starmask'] is None and 'savemask' in kwargs and kwargs['savemask']:
            header = fits.Header()
            header['IMAGE 1'] = 'star mask'
            header['IMAGE 2'] = 'overflow values mask'
            if not results['background'] is None:
                for key in results['background'].keys():
                    header['bk %s' % key] = str(results['background'][key])
            hdul = fits.HDUList([fits.PrimaryHDU(header=header),
                                 fits.ImageHDU(results['starmask']['mask'].astype(int)),
                                 fits.ImageHDU(results['starmask']['overflow mask'].astype(int))])
            hdul.writeto(saveto + name + '_mask.fits', overwrite = True)
            sleep(1)
            # Zip the mask file because it can be large and take a lot of memory, but in principle
            # is very easy to compress
            os.system('gzip -fq '+ saveto + name + '_mask.fits')
            
    def Process_Image(self, IMG, pixscale, saveto = None, name = None, kwargs_internal = {}, **kwargs):
        """
        Function which runs the pipeline for a single image. Each sub-function of the pipeline is run
        in order and the outputs are passed along. If multiple images are given, the pipeline is
        excecuted on the first image and the isophotes are applied to the others.
        
        IMG: string or list of strings providing the path to an image file
        pixscale: angular size of the pixels in arcsec/pixel
        saveto: string or list of strings indicating where to save profiles
        name: string name of galaxy in image, used for log files to make searching easier
        overflowval: pixel flux value for oversaturated pixels

        returns 0 for successful completion of processing
        """

        kwargs.update(kwargs_internal)
        
        # use filename if no name is given
        if name is None:
            name = IMG[(IMG.rfind('/') if '/' in IMG else 0):IMG.find('.', (IMG.rfind('/') if '/' in IMG else 0))]

        # Read the primary image
        dat = Read_Image(IMG, **kwargs)
            
        # Check that image data exists and is not corrupted
        if dat is None or np.all(dat[int(len(dat)/2.)-10:int(len(dat)/2.)+10, int(len(dat[0])/2.)-10:int(len(dat[0])/2.)+10] == 0):
            logging.error('%s Large chunk of data missing, impossible to process image' % name)
            return 1
        # Save profile to the same folder as the image if no path is provided
        if saveto is None:
            saveto = './'
            
        # Track time to run analysis
        start = time()
        
        # Run the Pipeline
        results = {}
        # try:
        if True:
            for step in range(len(self.pipeline_steps)):
                logging.info('%s: %s at: %.1f sec' % (name, self.pipeline_steps[step], time() - start))
                results[self.pipeline_steps[step]] = self.pipeline_functions[self.pipeline_steps[step]](dat, pixscale, name, results, **kwargs)
        # except Exception as e:
        #     logging.error('%s: %s' % (name, str(e)))
        #     return 1

        # Save the profile
        logging.info('%s: saving at: %.1f sec' % (name, time() - start))
        self.WriteProf(results, saveto, pixscale, name = name, **kwargs)
                
        logging.info('%s: Processing Complete! (at %.1f sec)' % (name, time() - start))
        return 0
    
    def Process_List(self, IMG, pixscale, n_procs = 4, saveto = None, name = None, **kwargs):
        """
        Wrapper function to run "Process_Image" in parallel for many images.
        
        filelist: list of strings or list of list of strings containing image file paths
        pixscale: angular pixel size in arcsec/pixel
        n_procs: number of processors to use
        saveto: list of strings or list of list of strings containing file paths to save profiles
        names: names of the galaxies, used for logging
        """
        
        # Format the inputs so that they can be zipped with the images files
        # and passed to the Process_Image function.
        if type(pixscale) in [float, int]:
            use_pixscale = [float(pixscale)]*len(IMG)
        else:
            use_pixscale = pixscale
        if saveto is None:
            use_saveto = [None]*len(IMG)
        else:
            use_saveto = saveto
        if name is None:
            use_name = [None]*len(IMG)
        else:
            use_name = name

        if all(type(v) != list for v in kwargs.values()):
            use_kwargs = [kwargs]*len(IMG)
        else:
            use_kwargs = []
            for i in range(len(IMG)):
                tmp_kwargs = {}
                for k in kwargs.keys():
                    if type(kwargs[k]) == list:
                        tmp_kwargs[k] = kwargs[k][i]
                    else:
                        tmp_kwargs[k] = kwargs[k]
                use_kwargs.append(tmp_kwargs)
        # Track how long it takes to run the analysis
        start = time()
        
        # Create a multiprocessing pool to parallelize image processing
        imagedata = list(zip(IMG, use_pixscale, use_saveto,
                             use_name, use_kwargs))
        if n_procs > 1:
            with Pool(n_procs) as pool:
                res = pool.starmap(self.Process_Image,
                                   imagedata,
                                   chunksize = 5 if len(IMG) > 100 else 1)
        else:
            res = list(starmap(self.Process_Image, imagedata))
            
        # Report completed processing, and track time used
        logging.info('All Images Finished Processing at %.1f' % (time() - start))
        
        # Return the success/fail indicators for every Process_Image excecution
        return res
        
    def Process_ConfigFile(self, config_file):

        # Import the config file regardless of where it is from
        import importlib
        use_config = config_file[:-3] if config_file[-3:] == '.py' else config_file
        if '/' in config_file:
            sys.path.append(config_file[:config_file.rfind('/')])
            use_config = use_config[use_config.rfind('/')+1:]
        try:
            c = importlib.import_module(use_config)
        except:
            sys.path.append(os.getcwd())
            c = importlib.import_module(use_config)

        if 'forced' in c.process_mode:
            self.UpdatePipeline(new_pipeline_functions = {'center': Center_Forced,
                                                          'starmask': Star_Mask_Given,
                                                          'isophotefit': Isophote_Fit_Forced,
                                                          'isophoteextract': Isophote_Extract_Forced,
                                                          'checkfit': Check_Fit_Simple})
            
        try:
            self.UpdatePipeline(new_pipeline_functions = c.pipeline_functions)
        except:
            pass
        try:
            self.UpdatePipeline(new_pipeline_steps = c.pipeline_steps)
        except:
            pass
            
        use_kwargs = GetKwargs(c)
            
        if c.process_mode in ['image', 'forced image']:
            return self.Process_Image(IMG = c.image_file, pixscale = c.pixscale, **use_kwargs)
        elif c.process_mode in ['image list', 'forced image list']:
            return self.Process_List(IMG = c.image_file, pixscale = c.pixscale, **use_kwargs)
        else:
            logging.error('Unrecognized process_mode! Should be in: [image, image list, forced image, forced image list]')
            return 1
        