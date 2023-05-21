#
#  DESCRIPTION   : Script for maple pipeline. Declares functions and classes that are used
#                   in several scripts throughout the pipeline
#
#  AUTHOR(S)     : Gordon Rix
#

import pandas as pd
import numpy as np
import pathlib
import holoviews as hv

from bokeh.io import export_svgs
from selenium import webdriver as wd
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from bokeh import palettes

# define colormaps
colormaps = {'NT':{'A':palettes.Greens[3][1], #take the middle color from the 3 length color list
                                'T':palettes.Reds[3][1],
                                'G':'#000000',           #black
                                'C':palettes.Blues[3][1],
                                '-':'#d3d3d3'}}           #grey

AAs_by_group = [['K','R','H'],              # positive, red
                ['D','E'],                  # negative, green
                ['F','Y','W'],              # aromatic, purple
                ['A','V','I','L'],          # small hydrophobic, blue
                ['C','M','S','T','N','Q']]  # sulfurous and polar uncharged, yellow->orange

colorDictList= [palettes.Reds,
                palettes.Greens,
                palettes.Purples,
                palettes.Blues,
                palettes.YlOrBr]

amino_acid_colormap = {}
for AAs,colorDict in zip(AAs_by_group,colorDictList):
    colorList = colorDict[len(AAs)+1][::-1]
    for i,AA in enumerate(AAs):
        amino_acid_colormap[AA] = colorList[i+1]
amino_acid_colormap.update({'P':'#FA11F2','G':'#FEFBEA','*':'#000000','-':'#d3d3d3'}) # pink and cream for proline and glycine, black for stop, grey for gap

colormaps.update({'AA':amino_acid_colormap})

def dist_to_DF(dist, x, y):
    """given a np.bincount output, i.e. a distribution of values in which each value is
    the number of observations corresponding to the value's position in the distribution
    (e.g. [0,3,1] is a distribution for 4 values in which 3 of the values were '1'),
    this will calculate the distribution as a proportion and as a cumulative proportion
    and produce a DataFrame from these three numpy arrays that describe the distribution
    
    dist:       np.array of shape (maximumValue)
    x, y:       strings that describes the x and y variable"""

    maxVal = dist.shape[0]
    dist = dist.reshape(1,maxVal)

    values = np.arange(maxVal).reshape(1,maxVal)
    proportion = np.divide(dist, dist.sum())
    cumsum = np.cumsum(proportion).reshape(1,maxVal)

    df = pd.DataFrame( np.concatenate((values, dist, proportion, cumsum), axis=0).T, columns = [x, f"total {y}", f"proportion of {y}", f"cumulative proportion of {y}"] )
    df[df.columns[[0,1]]] = df[df.columns[[0,1]]].apply(lambda x: x.astype(int)) # convert x value and total counts columns to int

    return df

def export_svg_plots(plots, file_name, labels=[]):
    """
    exports individual bokeh plots from a list of holoviews plots
    with the provided file name and the index of the plot in the list
    
    plots:      list of holoviews bokeh plots
    file_name   file name being used to save the plots. must end in '.html'
    labels      list of strings to be appended to the end of the file name for 
                    each of the plots, which are exported as separate files
    """

    options = wd.ChromeOptions()
    options.add_argument('--headless')
    options.add_argument('--disable-gpu')
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=2000x2000")
    options.add_argument('--disable-dev-shm-usage')

    service = Service(ChromeDriverManager().install())
    webdriver = wd.Chrome(service=service, options=options)

    # unless labels are provided, name plots just using their order in the plot list
    if not labels:
        labels = [i for i in range(0,len(plots))]
    file_name_base = file_name[:-5]

    pathlib.Path(file_name_base).parent.absolute().mkdir(parents=True, exist_ok=True)
    
    for plot, label in zip(plots, labels):
        fName = f'{file_name_base}_{label}.svg'
        p = hv.render(plot,backend='bokeh')
        p.output_backend='svg'
        export_svgs(p, 
            filename=fName,
            webdriver=webdriver)
        
def conspicuous_mutations(df, num_positions, colormap, most_common=True, heatmap=False):
    """
    produces a bar plot of the most or least frequent mutations
    
    parameters:
        df (pd.DataFrame):   dataframe of aggregated mutations output by aggregate_mutations
        num_positions (int): number of mutations to include in the bar plot output
        colormap (dict):     AA/NT letter : color hex code key:value pairs to use for the plot
        most_common (bool):  if True/False, output the most/least commonly mutated positions
        
    returns:
        hv.Bars or hv.heatmap object showing the topN most frequently observed mutations
            in the aggregated mutations dataframe
    """
    
    df = df.sort_values(['total_count','position'], ascending=[(not most_common),True])
    df_grouped = df.groupby('position', as_index=False).sum().sort_values(['total_count','position'],ascending=[not most_common, True])
    positions = df_grouped['position'].iloc[:num_positions]
    df = df[df['position'].isin(positions)]
    df = df.sort_values('position', ascending=True)
    df['position'] = df['wt'] + df['position'].astype(str)
    if heatmap:
        plot = hv.HeatMap(df, kdims=['position','mutation'], vdims=['proportion_of_seqs', 'total_count']).opts(
                    show_legend=True, height=500, xlabel='position',
                    xrotation=40, cmap=colormap, tools=['hover'])
    else:
        plot = hv.Bars(df, kdims=['position','mutation'], vdims=['proportion_of_seqs', 'total_count']).opts(
                    show_legend=False, height=500, xlabel='position',
                    xrotation=40, stacked=True, cmap=colormap, tools=['hover'])
    return plot